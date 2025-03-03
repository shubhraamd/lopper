#/*
# * Copyright (c) 2019,2020 Xilinx Inc. All rights reserved.
# *
# * Author:
# *       Bruce Ashfield <bruce.ashfield@xilinx.com>
# *
# * SPDX-License-Identifier: BSD-3-Clause
# */

import copy
import struct
import sys
import types
import unittest
import os
import getopt
import re
import subprocess
import shutil
from pathlib import Path
from pathlib import PurePath
from io import StringIO
import contextlib
import importlib
from lopper import Lopper
from lopper import LopperFmt
import lopper
from lopper.tree import *
from re import *
from enum import Enum
from enum import IntEnum
from string import Template

sys.path.append(os.path.dirname(__file__))
from openamp_xlnx_common import *

RPU_PATH = "/rpu@ff9a0000"
REMOTEPROC_D_TO_D = "openamp,remoteproc-v1"
RPMSG_D_TO_D = "openamp,rpmsg-v1"
output_file = "amd_platform_info.h"

class CPU_CONFIG(IntEnum):
    RPU_LOCKSTEP = 0
    RPU_SPLIT = 1

class RPU_CORE(IntEnum):
    RPU_0 = 0
    RPU_1 = 1
    RPU_2 = 2
    RPU_3 = 3

# This is used for YAML representation
# after this is parsed, the above enums are used for internal record keeping.
class CLUSTER_CONFIG(Enum):
    RPU_LOCKSTEP = 0
    RPU_0 = 1
    RPU_1 = 2

def is_compat( node, compat_string_to_test ):
    if re.search( "openamp,xlnx-rpu", compat_string_to_test):
        return xlnx_openamp_rpu
    return ""

def xlnx_rpmsg_native_update_carveouts(tree, elfload_node,
                                       native_shm_mem_area_start, native_shm_mem_area_size,
                                       native_amba_shm_node):
    # start and size of reserved-mem rproc0
    start = elfload_node.props("start")[0].value
    size = elfload_node.props("size")[0].value

    # update size if applicable
    if start < native_shm_mem_area_start:
        native_shm_mem_area_start = start
    native_shm_mem_area_size += size

    # update rproc0 size
    elfload_res_mem_node = tree["/reserved-memory/" + elfload_node.name]
    elfload_res_mem_reg = elfload_res_mem_node.props("reg")[0].value
    elfload_res_mem_reg[3] = native_shm_mem_area_size
    elfload_res_mem_node.props("reg")[0].value = elfload_res_mem_reg

    # FIXME: update demos so that start of shm is not hard-coded offset
    native_shm_mem_area_start += 0x20000
    shm_space = [0, native_shm_mem_area_start, 0, native_shm_mem_area_size]
    native_amba_shm_node + LopperProp(name="reg", value=shm_space)

    return True


native_shm_node_count = 0
def xlnx_rpmsg_construct_carveouts(tree, carveouts, rpmsg_carveouts, native,
                                   amba_node = None, elfload_node = None, verbose = 0 ):
    global native_shm_node_count
    res_mem_node = tree["/reserved-memory"]
    native_amba_shm_node = None
    native_shm_mem_area_size = 0
    native_shm_mem_area_start = 0xFFFFFFFF

    if amba_node != None:
        native_amba_shm_node = LopperNode(-1, amba_node.abs_path + "/shm" + str(native_shm_node_count))
        native_amba_shm_node + LopperProp(name="compatible", value="shm_uio")
        tree.add(native_amba_shm_node)
        tree.resolve()
        native_shm_node_count += 1


    # only applicable for DDR carveouts
    for carveout in carveouts:
        # SRAM banks have status prop
        # SRAM banks are not in reserved memory
        # FIXME  skip SRAM banks in RPMsg support
        if carveout.props("status") != []:
            continue
        elif carveout.props("no-map") != []:
            start = carveout.props("start")[0].value
            size = carveout.props("size")[0].value
            # handle native RPMsg
            if native:
                if start < native_shm_mem_area_start:
                    native_shm_mem_area_start = start
                native_shm_mem_area_size += size
                # add more space for shared buffers
                if 'vdev0buffer' in carveout.name:
                    native_shm_mem_area_size += size
            else:

                new_node =  LopperNode(-1, "/reserved-memory/"+carveout.name)
                new_node + LopperProp(name="no-map")
                new_node + LopperProp(name="reg", value=[0, start, 0, size])

                if "vdev0buffer" in carveout.name:
                    new_node + LopperProp(name="compatible", value="shared-dma-pool")

                tree.add(new_node)
                tree.resolve()

                if new_node.phandle == 0:
                    new_node.phandle = new_node.phandle_or_create()

                new_node + LopperProp(name="phandle", value = new_node.phandle)
                rpmsg_carveouts.append(new_node)

        else:
            print("WARNING: invalid remoteproc elfload carveout", carveout)
            return False


    if native:
        ret = xlnx_rpmsg_native_update_carveouts(tree, elfload_node,
                                                 native_shm_mem_area_start, native_shm_mem_area_size,
                                                 native_amba_shm_node)
        if not ret:
            return ret

    return True


def xlnxl_rpmsg_ipi_get_ipi_id(tree, ipi, role):
    ipi_node = None
    ipi_id_prop_name = "xlnx,ipi-id"
    ipi_node = tree.pnode( ipi )

    if ipi_node == None:
        print("ERROR: Unable to find ipi: ", ipi, " for role: ", role)
        return False

    ipi_id = ipi_node.props(ipi_id_prop_name)
    if ipi_id == []:
        print("WARNING: Unable to find IPI ID for ", ipi)
        return False

    return ipi_id[0]


def xlnx_rpmsg_ipi_parse_per_channel(remote_ipi, host_ipi, tree, node, openamp_channel_info,
                                     remote_node, channel_id, native, channel_index, 
                                     verbose = 0):
    ipi_id_prop_name = "xlnx,ipi-id"

    host_ipi_id = xlnxl_rpmsg_ipi_get_ipi_id(tree, host_ipi[channel_index], "host")

    if host_ipi_id == False:
        return host_ipi_id

    remote_ipi_id = xlnxl_rpmsg_ipi_get_ipi_id(tree, remote_ipi, "remote")
    if remote_ipi_id == False:
        return remote_ipi_id

    host_ipi = tree.pnode( host_ipi[channel_index])


    # find host to remote buffers
    host_to_remote_ipi_channel = None
    for subnode in host_ipi.subnodes():
        subnode_ipi_id = subnode.props(ipi_id_prop_name)
        if subnode_ipi_id != [] and remote_ipi_id.value[0] == subnode_ipi_id[0].value[0]:
            host_to_remote_ipi_channel = subnode
    if host_to_remote_ipi_channel == None:
        print("WARNING no host to remote IPI channel has been found.")
        return False

    remote_ipi = tree.pnode( remote_ipi )

    # find remote to host buffers
    remote_to_host_ipi_channel = None
    for subnode in remote_ipi.subnodes():
        subnode_ipi_id = subnode.props(ipi_id_prop_name)
        if subnode_ipi_id != [] and host_ipi_id.value[0] == subnode_ipi_id[0].value[0]:
            remote_to_host_ipi_channel = subnode
    if remote_to_host_ipi_channel == None:
        print("WARNING no remote to host IPI channel has been found.")
        return False

    # store IPI IRQ Vector IDs
    platform =  openamp_channel_info["platform"]
    host_ipi_base = host_ipi.props("reg")[0][1]
    remote_ipi_base = remote_ipi.props("reg")[0][1]
    soc_strs = {
      SOC_TYPE.ZYNQMP: "ZU+",
      SOC_TYPE.VERSAL: "Versal",
      SOC_TYPE.VERSAL_NET: "Versal NET",
    }

    irq_vect_ids = {
      SOC_TYPE.ZYNQMP: zynqmp_ipi_to_irq_vect_id,
      SOC_TYPE.VERSAL: versal_ipi_to_irq_vect_id,
      SOC_TYPE.VERSAL_NET: versal_net_ipi_to_irq_vect_id,
    }

    irq_vect_id_map = irq_vect_ids[platform]
    soc_str = soc_strs[platform]


    if platform in [SOC_TYPE.ZYNQMP, SOC_TYPE.VERSAL, SOC_TYPE.VERSAL_NET]:
        if host_ipi_base not in irq_vect_id_map.keys():
            print("WARNING: host IPI", hex(host_ipi_base), "not in IRQ VECTOR ID Mapping for ", soc_str)
            return False
        if remote_ipi_base not in irq_vect_id_map.keys():
            print("WARNING: remote IPI", hex(remote_ipi_base), "not in IRQ VECTOR ID Mapping for ", soc_str)
            return False

        openamp_channel_info["host_ipi_base"+channel_id] = host_ipi_base
        openamp_channel_info["host_ipi_irq_vect_id"+channel_id] = irq_vect_id_map[host_ipi_base]
        openamp_channel_info["remote_ipi_base"+channel_id] = remote_ipi_base
        openamp_channel_info["remote_ipi_irq_vect_id"+channel_id] = irq_vect_id_map[remote_ipi_base]
    else:
        print("Unsupported platform")

    openamp_channel_info["host_ipi_"+channel_id] = host_ipi
    openamp_channel_info["remote_ipi_"+channel_id] = remote_ipi
    openamp_channel_info["rpmsg_native_"+channel_id] = native
    openamp_channel_info["host_to_remote_ipi_channel_" + channel_id] = host_to_remote_ipi_channel
    openamp_channel_info["remote_to_host_ipi_channel_" + channel_id] = remote_to_host_ipi_channel
    return True


def xlnx_rpmsg_ipi_parse(tree, node, openamp_channel_info,
                         remote_node, channel_id, native, channel_index, 
                         verbose = 0 ):
    carveouts_prop = node.props("carveouts")
    amba_node = None
    ipi_id_prop_name = "xlnx,ipi-id"
    host_to_remote_ipi = None
    remote_to_host_ipi = None

    # collect host ipi
    host_ipi_prop = node.props("mbox")
    if host_ipi_prop == []:
        print("WARNING: ", node, " is missing mbox property")
        return False

    host_ipi_prop = host_ipi_prop[0].value
    remote_rpmsg_relation = None
    try:
        remote_rpmsg_relation = tree[remote_node.abs_path + "/domain-to-domain/rpmsg-relation"]
    except:
        print("WARNING: ", remote_node, " is missing rpmsg relation")
        return False

    # collect remote ipi
    remote_ipi_prop = remote_rpmsg_relation.props("mbox")
    if remote_ipi_prop == []:
        print("WARNING: ", remote_node, " is missing mbox property")
        return False

    remote_ipi_prop = remote_ipi_prop[0].value

    ret = xlnx_rpmsg_ipi_parse_per_channel(remote_ipi_prop[0], host_ipi_prop, tree, node, openamp_channel_info,
                                           remote_node, channel_id, native, channel_index, verbose)
    if ret != True:
        return False

    return True


def xlnx_rpmsg_setup_host_controller(tree, controller_parent, gic_node_phandle,
                                     host_ipi):
    controller_parent + LopperProp(name="compatible",value="xlnx,zynqmp-ipi-mailbox")
    controller_parent + LopperProp(name="interrupt-parent", value = [gic_node_phandle])
    controller_parent + LopperProp(name="#address-cells",value=1)
    controller_parent + LopperProp(name="#size-cells",value=1)
    controller_parent + LopperProp(name="ranges")

    host_interrupts_prop = host_ipi.props("interrupts")
    if host_interrupts_prop == []:
        print("WARNING: host ipi ", host_ipi, " missing interrupts property")
        return False

    host_interrupts_pval = host_interrupts_prop[0].value
    controller_parent + LopperProp(name="interrupts", value = copy.deepcopy(host_interrupts_pval))
    controller_parent + LopperProp(name="xlnx,ipi-id", value = host_ipi.props("xlnx,ipi-id")[0].value)

    tree.add(controller_parent)
    tree.resolve()

    if controller_parent.phandle == 0:
        controller_parent.phandle = controller_parent.phandle_or_create()

    controller_parent + LopperProp(name="phandle", value = controller_parent.phandle)

    return True


controller_parent_index = 1
def xlnx_rpmsg_create_host_controller(tree, host_ipi, gic_node_phandle, verbose = 0):
    global controller_parent_index
    controller_parent = None
    ctr_parent_name = "/zynqmp_ipi" + str(controller_parent_index)
    setup_host_controller = True
    try:
        controller_parent = tree[ctr_parent_name]

        ctr_parent_ipi_id = controller_parent.props("xlnx,ipi-id")
        if ctr_parent_ipi_id != [] and ctr_parent_ipi_id[0].value[0] == host_ipi.props("xlnx,ipi-id")[0].value[0]:
            setup_host_controller = False
        else:
            setup_host_controller = True
            controller_parent_index += 1
            # make new parent controller with updated name
            ctr_parent_name = "/zynqmp_ipi" + str(controller_parent_index)
            controller_parent = LopperNode(-1, ctr_parent_name)
    except:
        controller_parent = LopperNode(-1, ctr_parent_name)

    # check if host controller needs to be setup before adding props to it
    if setup_host_controller:
        ret = xlnx_rpmsg_setup_host_controller(tree, controller_parent,
                                               gic_node_phandle, host_ipi)
        if not ret:
            return False

    return controller_parent


def xlnx_rpmsg_native_update_ipis(tree, amba_node, openamp_channel_info, gic_node_phandle,
                                  amba_ipi_node_index, host_ipi):
    amba_node = openamp_channel_info["amba_node"]
    amba_ipi_node = LopperNode(-1, amba_node.abs_path + "/openamp_ipi" + str(amba_ipi_node_index))
    amba_ipi_node + LopperProp(name="compatible",value="ipi_uio")
    amba_ipi_node + LopperProp(name="interrupts",value=copy.deepcopy(host_ipi.props("interrupts")[0].value))
    amba_ipi_node + LopperProp(name="interrupt-parent",value=[gic_node_phandle])
    reg_val = copy.deepcopy(host_ipi.props("reg")[0].value)
    reg_val[3] = 0x1000
    amba_ipi_node + LopperProp(name="reg",value=reg_val)
    tree.add(amba_ipi_node)
    tree.resolve()
    amba_ipi_node_index += 1

    # check if there is a kernelspace IPI that has same interrupts
    # if so then error out
    for n in tree["/"].subnodes():
        compat_str = n.props("compatible")
        if compat_str != [] and compat_str[0].value == "xlnx,zynqmp-ipi-mailbox" and \
            n.props("interrupts")[0].value[1] ==  host_ipi.props("interrupts")[0].value[1]:
            # check if subnode exists with the correct structure
            # before erroring out. There may be IP IPIs that this
            # catches
            for sub_n in n.subnodes():
                if sub_n.props("reg-names") != []:
                    print("WARNING:  conflicting userspace and kernelspace for host ipi: ", host_ipi)
                    return False

    return True


def xlnx_rpmsg_kernel_update_mboxes(tree, host_ipi, remote_ipi, gic_node_phandle,
                                  openamp_channel_info, channel_id,
                                  core_node, remote_controller_node, rpu_core):
    remote_controller_node + LopperProp(name="phandle", value = remote_controller_node.phandle)
    remote_controller_node + LopperProp(name="#mbox-cells", value = 1)
    remote_controller_node + LopperProp(name="xlnx,ipi-id", value = remote_ipi.props("xlnx,ipi-id")[0].value)
    reg_names_val = [ "local_request_region", "local_response_region",
                      "remote_request_region","remote_response_region"]
    remote_controller_node + LopperProp(name="reg-names", value = reg_names_val)

    cpu_config = str(openamp_channel_info["cpu_config"+channel_id].value)
    host_to_remote_ipi_channel = openamp_channel_info["host_to_remote_ipi_channel_"+channel_id]
    remote_to_host_ipi_channel= openamp_channel_info["remote_to_host_ipi_channel_"+channel_id]
    response_buf_str = "xlnx,ipi-rsp-msg-buf"
    request_buf_str = "xlnx,ipi-req-msg-buf"

    host_remote_response = host_to_remote_ipi_channel.props(response_buf_str)
    if host_remote_response == []:
        print("WARNING: host_remote_response ", host_to_remote_ipi_channel, "is missing property name: ", response_buf_str)
        return False
    host_remote_request = host_to_remote_ipi_channel.props(request_buf_str)
    if host_remote_request == []:
        print("WARNING: host_remote_request ", host_to_remote_ipi_channel, " is missing property name: ", request_buf_str)
        return False
    remote_host_response = remote_to_host_ipi_channel.props(response_buf_str)
    if remote_host_response == []:
        print("WARNING: remote_host_response ", remote_to_host_ipi_channel, " is missing property name: ", response_buf_str)
        return False
    remote_host_request = remote_to_host_ipi_channel.props(request_buf_str)
    if remote_host_request == []:
        print("WARNING: remote_host_request ", remote_to_host_ipi_channel, " is missing property name: ", request_buf_str)
        return False

    host_remote_response = host_remote_response[0].value[0]
    host_remote_request = host_remote_request[0].value[0]
    remote_host_response = remote_host_response[0].value[0]
    remote_host_request = remote_host_request[0].value[0]

    remote_controller_node + LopperProp(name="reg", value = [
                                                         host_remote_request,  0x20,
                                                         host_remote_response, 0x20,
                                                         remote_host_request,  0x20,
                                                         remote_host_response, 0x20])

    core_node + LopperProp(name="mboxes", value = [remote_controller_node.phandle, 0, remote_controller_node.phandle, 1])
    return True

def xlnx_rpmsg_kernel_update_ipis(tree, host_ipi, remote_ipi, gic_node_phandle,
                                  core_node, openamp_channel_info, channel_id):
    controller_parent = xlnx_rpmsg_create_host_controller(tree, host_ipi, gic_node_phandle)

    # check if there is a userspace IPI that has same interrupts
    # if so then error out
    for n in tree["/"].subnodes():
        compat_str = n.props("compatible")
        if compat_str != [] and compat_str[0].value == "ipi_uio" and \
            n.props("interrupts")[0].value[1] ==  host_ipi.props("interrupts")[0].value[1]:
            print("WARNING:  conflicting userspace and kernelspace for host ipi: ", host_ipi)
            return False

    if controller_parent == False:
        return False
    rpu_core = openamp_channel_info["rpu_core" + channel_id]
    if not isinstance(rpu_core, str):
        rpu_core = str(rpu_core.value)

    remote_controller_node = LopperNode(-1, controller_parent.abs_path + "/ipi_mailbox_rpu" + rpu_core)
    tree.add(remote_controller_node)
    tree.resolve()

    if remote_controller_node.phandle == 0:
        remote_controller_node.phandle = remote_controller_node.phandle_or_create()


    return xlnx_rpmsg_kernel_update_mboxes(tree, host_ipi, remote_ipi, gic_node_phandle,
                                           openamp_channel_info, channel_id,
                                           core_node, remote_controller_node, rpu_core)


amba_ipi_node_index = 0
def xlnx_rpmsg_update_ipis(tree, host_ipi, remote_ipi, core_node, channel_id, openamp_channel_info, verbose = 0 ):
    global amba_ipi_node_index
    native = openamp_channel_info["rpmsg_native_"+ channel_id]
    platform = openamp_channel_info["platform"]
    controller_parent = None
    amba_node = None
    gic_node_phandle = None

    if platform == SOC_TYPE.VERSAL:
        gic_node_phandle = tree["/apu-bus/interrupt-controller@f9000000"].phandle
    elif platform == SOC_TYPE.VERSAL_NET:
        gic_node_phandle = tree["/apu-bus/interrupt-controller@e2000000"].phandle
    elif platform == SOC_TYPE.ZYNQMP:
        gic_node_phandle = tree["/apu-bus/interrupt-controller@f9010000"].phandle
    elif platform == SOC_TYPE.ZYNQ:
        gic_node_phandle = tree["/axi/interrupt-controller@f8f01000"].phandle
        core_node + LopperProp(name="interrupt-parent",value=[gic_node_phandle])
        return True
    else:
        print("invalid platform")
        return False

    if native:
        return xlnx_rpmsg_native_update_ipis(tree, amba_node, openamp_channel_info, gic_node_phandle,
                                             amba_ipi_node_index, host_ipi)
    else:
        return xlnx_rpmsg_kernel_update_ipis(tree, host_ipi, remote_ipi, gic_node_phandle,
                                             core_node, openamp_channel_info, channel_id)


def xlnx_rpmsg_update_tree(tree, node, channel_id, openamp_channel_info, verbose = 0 ):
    platform = openamp_channel_info["platform"]
    cpu_config = None
    host_ipi = None
    remote_ipi = None
    rpu_core = None
    carveouts_nodes = openamp_channel_info["carveouts_"+ channel_id]
    amba_node = None
    native = False
    rpmsg_carveouts = []

    if platform in [ SOC_TYPE.VERSAL, SOC_TYPE.ZYNQMP, SOC_TYPE.VERSAL_NET ]:
        native = openamp_channel_info["rpmsg_native_"+ channel_id]
        cpu_config =  openamp_channel_info["cpu_config"+channel_id]
        host_ipi = openamp_channel_info["host_ipi_"+ channel_id]
        remote_ipi = openamp_channel_info["remote_ipi_"+ channel_id]
        rpu_core = openamp_channel_info["rpu_core" + channel_id]

    elfload_node = None
    if native:
        amba_node = openamp_channel_info["amba_node"]

    # if Amba node exists, then this is for RPMsg native.
    # in this case find elfload node in case of native RPMsg as it may be contiguous
    # for AMBA Shm Node
    if native:
        for node in openamp_channel_info["elfload"+ channel_id]:
            if node.props("start") != []:
                elfload_node = node

    ret = xlnx_rpmsg_construct_carveouts(tree, carveouts_nodes, rpmsg_carveouts, native,
                                         amba_node=amba_node, elfload_node=elfload_node, verbose=verbose)
    if ret == False:
        return ret

    if platform in [ SOC_TYPE.VERSAL, SOC_TYPE.ZYNQMP ]:
        core_node_path = tree["/rf5ss@ff9a0000/r5f_" + str(rpu_core)]
        core_node = tree[core_node_path]
    elif platform == SOC_TYPE.VERSAL_NET:
        core_node_path = "/rf52ss_"

        cluster = "0"
        rpu_cfg_reg = 0xFF9A0100
        if int(rpu_core) > int(RPU_CORE.RPU_1):
            cluster = "1"
            rpu_cfg_reg = 0xFF9A0200

        core_node_path += cluster + "@" + hex(rpu_cfg_reg).replace("0x","") + "/r52f_" + str(rpu_core)
        core_node = tree[core_node_path]
    else:
        core_node = tree["/remoteproc@0"]

    mem_region_prop = core_node.props("memory-region")[0]

    # add rpmsg carveouts to cluster core node if using rpmsg kernel driver
    new_mem_region_prop_val = mem_region_prop.value
    if not native:
        for rc in rpmsg_carveouts:
            new_mem_region_prop_val.append(rc.phandle)
        # update property with new values
        mem_region_prop.value = new_mem_region_prop_val

    ret = xlnx_rpmsg_update_ipis(tree, host_ipi, remote_ipi, core_node, channel_id, openamp_channel_info, verbose)
    if ret != True:
        return False

    return True


def xlnx_construct_text_file(openamp_channel_info, channel_id, role, verbose = 0 ):
    text_file_contents = ""
    rpmsg_native = openamp_channel_info["rpmsg_native_"+channel_id]
    carveouts = openamp_channel_info["carveouts_"+channel_id]
    elfload = openamp_channel_info["elfload"+channel_id]
    platform = openamp_channel_info["platform"]
    tx = None
    rx = None
    SHARED_BUF_PA = 0
    SHARED_BUF_SIZE = 0
    inputs = None

    for c in carveouts:
        base = hex(c.props("start")[0].value)
        size = hex(c.props("size")[0].value)
        name = ""
        if "vring0" in c.name:
            name = "VRING0"
            tx = base
        elif "vring1" in c.name:
            name = "VRING1"
            rx = base
        else:
            name = "VDEV0BUFFER"
            SHARED_BUF_PA = base
            SHARED_BUF_SIZE = size

    if not rpmsg_native:
        tx = "FW_RSC_U32_ADDR_ANY"
        rx = "FW_RSC_U32_ADDR_ANY"

    elfbase = None
    elfsize = None
    SHARED_MEM_PA = 0
    SHARED_BUF_OFFSET = 0
    RSC_MEM_PA = 0
    for e in elfload:
        if e.props("start") != []: # filter to only parse ELF LOAD node
            elfbase = hex(e.props("start")[0].value)
            elfsize = hex(e.props("size")[0].value)
            RSC_MEM_PA = hex(e.props("start")[0].value + 0x20000)
            SHARED_MEM_PA = hex(e.props("start")[0].value + e.props("size")[0].value)
            SHARED_BUF_OFFSET = hex( e.props("size")[0].value * 2 )
            break

    SHM_DEV_NAME = "\"" + RSC_MEM_PA + '.shm\"'

    template = None
    irq_vect_ids = {
      SOC_TYPE.ZYNQMP: zynqmp_ipi_to_irq_vect_id,
      SOC_TYPE.VERSAL: versal_ipi_to_irq_vect_id,
      SOC_TYPE.VERSAL_NET: versal_net_ipi_to_irq_vect_id,
    }

    if platform in [ SOC_TYPE.ZYNQMP, SOC_TYPE.VERSAL, SOC_TYPE.VERSAL_NET ]:
        host_ipi = openamp_channel_info["host_ipi_" + channel_id]
        host_ipi_bitmask = hex(host_ipi.props("xlnx,ipi-bitmask")[0].value[0])
        host_ipi_irq_vect_id = hex(openamp_channel_info["host_ipi_irq_vect_id" + channel_id])
        host_ipi_base = hex(openamp_channel_info["host_ipi_base"+channel_id])

        remote_ipi = openamp_channel_info["remote_ipi_" + channel_id]
        remote_ipi_bitmask = hex(remote_ipi.props("xlnx,ipi-bitmask")[0].value[0])
        remote_ipi_irq_vect_id = hex(openamp_channel_info["remote_ipi_irq_vect_id" + channel_id])
        remote_ipi_base = hex(openamp_channel_info["remote_ipi_base"+channel_id])

        # update IPIs for remote role on Versal NET
        soc_ipi_map = irq_vect_ids[platform]
        if platform == SOC_TYPE.VERSAL_NET and role == 'remote':
            for key in soc_ipi_map.keys():
                value = soc_ipi_map[key]
                soc_ipi_map[key] = value + 32

            remote_ipi_irq_vect_id = int(remote_ipi_irq_vect_id, 16)
            remote_ipi_irq_vect_id += 32
            remote_ipi_irq_vect_id = hex(remote_ipi_irq_vect_id)

            no_buf_offset = 0x1000
            start_no_buf = 96
            end_no_buf = 101
            for nobuf_ipi in range(start_no_buf, end_no_buf + 1):
                nobuf_ipi_key = 0xEB3B0000 + no_buf_offset * (nobuf_ipi - start_no_buf)
                soc_ipi_map[nobuf_ipi_key] = nobuf_ipi

        IPI_IRQ_VECT_ID = remote_ipi_irq_vect_id if role == 'remote' else host_ipi_irq_vect_id
        POLL_BASE_ADDR = remote_ipi_base if role == 'remote' else host_ipi_base
        # flip this as we are kicking other side with the bitmask value
        IPI_CHN_BITMASK = host_ipi_bitmask if role == 'remote' else remote_ipi_bitmask

        # Add IPI Info for convenience
        EXTRAS = "\n"
        for key in soc_ipi_map.keys():
            EXTRAS += "#define IPI_" + str(soc_ipi_map[key]) + "_BASE_ADDR " + hex(key) + "UL\n"
            EXTRAS += "#define IPI_" + str(soc_ipi_map[key]) + "_VECT_ID " + str(soc_ipi_map[key]) + "U\n"

        template = platform_info_header_r5_template
        inputs = {
            "POLL_BASE_ADDR":POLL_BASE_ADDR,
            "SHM_DEV_NAME":SHM_DEV_NAME,
            "RSC_MEM_SIZE":'0x2000UL',
            "RSC_MEM_PA":RSC_MEM_PA,
            "DEV_BUS_NAME":"\"generic\"",
            "IPI_DEV_NAME":"\"ipi\"",
            "IPI_IRQ_VECT_ID":IPI_IRQ_VECT_ID,
            "IPI_CHN_BITMASK":IPI_CHN_BITMASK,
            "RING_TX":tx,
            "RING_RX":rx,
            "SHARED_MEM_PA": SHARED_MEM_PA,
            "SHARED_MEM_SIZE":"0x100000UL",
            "SHARED_BUF_OFFSET":SHARED_BUF_OFFSET,
            "SHARED_BUF_PA":SHARED_BUF_PA,
            "SHARED_BUF_SIZE":SHARED_BUF_SIZE,
            "EXTRAS":EXTRAS,
        }
    elif platform == SOC_TYPE.ZYNQ:
        inputs = {
            "RING_TX":tx,
            "RING_RX":rx,
            "SHARED_MEM_PA": SHARED_MEM_PA,
            "SHARED_MEM_SIZE":"0x100000UL",
            "SHARED_BUF_OFFSET":SHARED_BUF_OFFSET,
            "SHARED_BUF_PA":SHARED_BUF_PA,
            "SHARED_BUF_SIZE":SHARED_BUF_SIZE,
            "SGI_TO_NOTIFY":15,
            "SGI_NOTIFICATION":14,
            "SCUGIC_DEV_NAME":"\"scugic_dev\"",
            "SCUGIC_BUS_NAME":"\"generic\"",
            "SCUGIC_PERIPH_BASE":"0xF8F00000UL",
        }

        template = platform_info_header_a9_template

    f = open(output_file, "w")
    output = Template(template)
    f.write(output.substitute(inputs))
    f.close()

    return True

def xlnx_rpmsg_parse_get_carveout_nodes(tree, carveout_prop, len_remote_nodes, column):
    channel_carveouts_nodes = []
    row_width = int(len(carveout_prop) / len_remote_nodes)
    for current_elfload in range(0,row_width):
        idx = row_width * column + current_elfload
        tmp_node = tree.pnode( carveout_prop[idx] )
        channel_carveouts_nodes.append ( tmp_node )

    return channel_carveouts_nodes

def xlnx_rpmsg_parse_generate_native_amba_node(tree):
    try:
        amba_node = tree["/axi"]
    except:
        amba_node = LopperNode(-1, "/axi")
        amba_node + LopperProp(name="u-boot,dm-pre-reloc")
        amba_node + LopperProp(name="ranges")
        amba_node + LopperProp(name="#address-cells", value = 2)
        amba_node + LopperProp(name="#size-cells", value = 2)
        tree.add(amba_node)
        tree.resolve()

    return amba_node

def xlnx_rpmsg_parse(tree, node, openamp_channel_info, options, verbose = 0 ):
    # Xilinx OpenAMP subroutine to collect RPMsg information from RPMsg
    # relation
    amba_node = None
    # skip rpmsg remote node which will link to its host via 'host' property
    if node.props("host") != []:
        return True

    platform = get_platform(tree)
    if platform == None:
        print("Unsupported platform: ", root_compat)
        return False
    openamp_channel_info["platform"] = platform

    # check for remote property
    if node.props("remote") == []:
        print("WARNING: ", node, "is missing remote property")
        return False
    remote_nodes = populate_remote_nodes(tree, node.props("remote")[0])

    carveout_prop = node.props("carveouts")[0]
    if carveout_prop == []:
        print("WARNING: ", node, " is missing carveouts property")
        return False

    carveout_prop = node.props("carveouts")[0]

    channel_ids = []

    for i, remote_node in enumerate(remote_nodes):
        channel_id = "_"+node.parent.parent.name+"_"+remote_node.name
        channel_carveouts_nodes = xlnx_rpmsg_parse_get_carveout_nodes(tree, carveout_prop, len(remote_nodes), i)
        openamp_channel_info["carveouts_"+channel_id] = channel_carveouts_nodes

        # rpmsg native?
        native = node.props("openamp-xlnx-native")
        if native != [] and native[0].value[0] == 1:
            native = True
            if native and platform == SOC_TYPE.ZYNQ:
                print("ERROR: Native RPMsg not supported for Zynq")
            # if native is true, then find and store amba bus
            # to store IPI and SHM nodes
            amba_node = xlnx_rpmsg_parse_generate_native_amba_node(tree)
            openamp_channel_info["amba_node"] = amba_node
        else:
            native = False

        # Zynq has hard-coded IPIs in driver
        if platform in [ SOC_TYPE.ZYNQMP, SOC_TYPE.VERSAL, SOC_TYPE.VERSAL_NET ]:
            ret = xlnx_rpmsg_ipi_parse(tree, node, openamp_channel_info,
                                 remote_node, channel_id, native, i, verbose)
            if ret != True:
                return False
        elif platform == SOC_TYPE.ZYNQ: # Zynq does not support rpmsg native
            openamp_channel_info["rpmsg_native_"+channel_id] = False
        ret = xlnx_rpmsg_update_tree(tree, node, channel_id, openamp_channel_info, verbose )
        if ret != True:
            return False

        channel_ids.append( channel_id )

    try:
        args = options['args']
    except:
        print("No arguments passed for OpenAMP Module. Need role property")
        return False

    # Here try for key value pair arguments
    opts,args2 = getopt.getopt( args, "l:m:n:pv", [ "verbose", "permissive", "openamp_role=", "openamp_host=", "openamp_remote=" ] )
    if opts == [] and args2 == []:
        print('WARNING: No arguments passed for OpenAMP Module. Erroring out now.')
        return False

    role = None
    arg_host = None
    arg_remote = None
    for o,a in opts:
        if o in ('-l', "--openamp_role"):
            role = a
        elif o in ('-m', "--openamp_host"):
            arg_host = a
        elif o in ('-n', "--openamp_remote"):
            arg_remote = a
        else:
            print("Argument: ",o, " is not recognized. Erroring out.")

    if role not in ['host', 'remote']:
        print('WARNING: Role value is not proper. Expect either "host" or "remote". Got: ', role)
        return False
    valid_core_inputs = []
    pattern = re.compile('_openamp_([0-9a-z]+_[0-9])_')
    for i in channel_ids:
        for j in pattern.findall(i):
            valid_core_inputs.append(j)
    valid_core_inputs = set(valid_core_inputs)

    if arg_host not in valid_core_inputs or arg_remote not in arg_remote:
        print('WARNING: OpenAMP Host or Remote value is not proper. Valid inputs are:', valid_core_inputs)
        return False

    chan_id = None
    for i in channel_ids:
        if arg_remote in i and arg_host in i:
            chan_id = i
    if chan_id == None:
        print("Unable to find channel with pair", arg_host, arg_remote)
        return False

    # Generate Text file to configure OpenAMP Application
    ret = xlnx_construct_text_file(openamp_channel_info, chan_id, role, verbose)
    if not ret:
        return ret

    # remove definitions
    defn_node =  tree["/definitions"]
    tree - defn_node

    return True


# tests for a bit that is set, going fro 31 -> 0 from MSB to LSB
def check_bit_set(n, k):
    if n & (1 << (k)):
        return True

    return False


def determine_cpus_config(remote_domain):
  cpus_prop_val = remote_domain.propval("cpus")
  cpu_config =  CLUSTER_CONFIG(cpus_prop_val[1]) # split or lockstep

  if cpus_prop_val != ['']:
    if len(cpus_prop_val) != 3:
      print("rpu cluster cpu prop invalid len")
      return -1

    if cpu_config in CLUSTER_CONFIG:
        if CLUSTER_CONFIG.RPU_LOCKSTEP == cpu_config:
            return CPU_CONFIG.RPU_LOCKSTEP
        else:
            return CPU_CONFIG.RPU_SPLIT

    # if here then no match
    print("WARNING: determine_cpus_config: invalid cpu config")
    return -1


def determinte_rpu_core(tree, cpu_config, remote_node):
    remote_cpus = remote_node.props("cpus")[0]

    if RPU_CORE(cpu_config) == CPU_CONFIG.RPU_LOCKSTEP:
        return RPU_CORE.RPU_0
    elif cpu_config == CPU_CONFIG.RPU_SPLIT:
        try:
            core_index = int(str(tree.pnode(remote_node.props("cpus")[0].value[0]))[-1])
            rpu_core_from_int = RPU_CORE(core_index)
            return rpu_core_from_int
        except:
            print("WARNING: determinte_rpu_core: invalid cpus for ", remote_node, cpu_config)
            return False
    else:
        print("WARNING: invalid cpu config: ", cpu_config)
        return False


def xlnx_remoteproc_construct_carveouts(tree, carveouts, new_ddr_nodes, verbose = 0 ):
    res_mem_node = None
    try:
        res_mem_node = tree["/reserved-memory"]
    except KeyError:
        res_mem_node = LopperNode(-1, "/reserved-memory")
        res_mem_node + LopperProp(name="#address-cells",value=2)
        res_mem_node + LopperProp(name="#size-cells",value=2)
        res_mem_node + LopperProp(name="ranges",value=[])
        tree.add(res_mem_node)

    # only applicable for DDR carveouts
    for carveout in carveouts:
        # SRAM banks have status prop
        # SRAM banks are not in reserved memory
        if carveout.props("status") != []:
            continue
        elif carveout.props("no-map") != []:
            start = carveout.props("start")[0].value
            size = carveout.props("size")[0].value
            new_node =  LopperNode(-1, "/reserved-memory/"+carveout.name)
            new_node + LopperProp(name="no-map")
            new_node + LopperProp(name="reg", value=[0, start, 0, size])
            tree.add(new_node)

            phandle_val = new_node.phandle_or_create()

            new_node + LopperProp(name="phandle", value = phandle_val)
            new_node.phandle = phandle_val
            new_ddr_nodes.append(new_node.phandle)
        else:
            print("WARNING: invalid remoteproc elfload carveout", carveout)
            return False

    return True


def xlnx_remoteproc_construct_cluster(tree, cpu_config, elfload_nodes, new_ddr_nodes, host_node, remote_node, openamp_channel_info, verbose = 0):
    channel_id = "_"+host_node.name+"_"+remote_node.name
    platform = openamp_channel_info["platform"]
    cluster_node = None
    rpu_core = openamp_channel_info["rpu_core"+channel_id]
    cluster_node_path = "/rf5ss@ff9a0000" # SOC_TYPE.VERSAL, SOC_TYPE.ZYNQMP
    cluster_reg = 0xff9a0000

    if platform == SOC_TYPE.VERSAL_NET:
        cluster_node_path = "/rf52ss_"
        cluster = "0"
        rpu_cfg_reg = 0xFF9A0100
        if int(rpu_core) > int(RPU_CORE.RPU_1):
            cluster = "1"
            rpu_cfg_reg = 0xFF9A0200

        cluster_node_path += cluster + "@" + hex(rpu_cfg_reg).replace("0x","")
        cluster_reg = rpu_cfg_reg

    if cpu_config in [ CPU_CONFIG.RPU_LOCKSTEP, CPU_CONFIG.RPU_SPLIT ]:

        try:
            cluster_node = tree[cluster_node_path]
        except KeyError:
            cluster_node = LopperNode(-1,cluster_node_path)
            cluster_node = LopperNode(-1, cluster_node_path)
            cluster_node + LopperProp(name="compatible", value = "xlnx,zynqmp-r5-remoteproc")
            cluster_node + LopperProp(name="#address-cells", value = 0x2)
            cluster_node + LopperProp(name="#size-cells", value = 0x2)
            cluster_node + LopperProp(name="ranges", value= [])
            cluster_node + LopperProp(name="xlnx,cluster-mode", value = cpu_config.value)
            cluster_node + LopperProp(name="reg", value = [0, cluster_reg, 0, 0x10000])
            tree.add(cluster_node)

        rpu_core = openamp_channel_info["rpu_core"+channel_id]
        rpu_core_pd_prop = openamp_channel_info["rpu_core_pd_prop"+channel_id]

        core_name = "r5f_" + rpu_core
        compatible_str = "xilinx,r5f"
        if platform == SOC_TYPE.VERSAL_NET:
            core_name = "r52f_" + str(rpu_core)
            compatible_str = "xilinx,r52f"

        core_node = LopperNode(-1, cluster_node_path + "/" + core_name)

        core_node + LopperProp(name="compatible", value = compatible_str)
        core_node + LopperProp(name="#address-cells", value = 2)
        core_node + LopperProp(name="#size-cells", value = 2)
        core_node + LopperProp(name="ranges", value=[])
        core_node + LopperProp(name="power-domain", value=copy.deepcopy(rpu_core_pd_prop.value))
        core_node + LopperProp(name="mbox-names", value = ["tx", "rx"]);
        tree.add(core_node)

        srams = []
        for carveout in elfload_nodes:
            if carveout.props("status") != []:
                srams.append(carveout.phandle)
                # FIXME for each sram, add 'power-domain' prop for kernel driver
                carveout + LopperProp(name="power-domain",
                                      value=copy.deepcopy( carveout.props("power-domains")[0].value ))

        core_node + LopperProp(name="sram", value=srams)
    elif platform == SOC_TYPE.ZYNQ:
        core_node = LopperNode(-1, "/remoteproc@0")
        core_node + LopperProp(name="compatible", value = "xlnx,zynq_remoteproc")
        core_node + LopperProp(name="firmware", value = "firmware")

        tree.add(core_node)

    else:
        return False

    # there may be new nodes created in linux reserved-memory node to account for
    memory_region = []
    for phandle_val in new_ddr_nodes:
        memory_region.append(phandle_val)
    core_node + LopperProp(name="memory-region", value=memory_region)

    return True

def xlnx_remoteproc_update_tree(tree, node, remote_node, openamp_channel_info, verbose = 0 ):
    channel_id = "_"+node.parent.parent.name+"_"+remote_node.name
    elfload_nodes = openamp_channel_info["elfload"+channel_id]
    new_ddr_nodes = []
    platform = openamp_channel_info["platform"]
    if platform in [SOC_TYPE.ZYNQMP, SOC_TYPE.VERSAL, SOC_TYPE.VERSAL_NET]:
        cpu_config =  openamp_channel_info["cpu_config"+channel_id]
    else:
        cpu_config = 0

    ret = xlnx_remoteproc_construct_carveouts(tree, elfload_nodes, new_ddr_nodes, verbose)
    if ret == False:
        return ret
    ret = xlnx_remoteproc_construct_cluster(tree, cpu_config, elfload_nodes, new_ddr_nodes, node.parent.parent, remote_node, openamp_channel_info, verbose = 0)
    if ret == False:
        return ret

    return True


def xlnx_remoteproc_rpu_parse(tree, node, openamp_channel_info, remote_node, elfload_nodes):
    cpu_config = determine_cpus_config(remote_node)
    platform = get_platform(tree)
    rpu_core = None

    if cpu_config in [ CPU_CONFIG.RPU_LOCKSTEP, CPU_CONFIG.RPU_SPLIT]:
        rpu_core = determinte_rpu_core(tree, cpu_config, remote_node )
        if rpu_core not in RPU_CORE:
            print("WARNING: Invalid rpu core: ", rpu_core, platform)
            return False
    else:
        print("WARNING: cpu_config: ", cpu_config, " is not in ", [ CPU_CONFIG.RPU_LOCKSTEP, CPU_CONFIG.RPU_SPLIT])
        return False
    rpu_cluster_node = tree.pnode(remote_node.props("cpus")[0].value[0])
    # For Versal NET all cores are under first core
    if platform == SOC_TYPE.VERSAL_NET:
        rpu_cluster_node_path = rpu_cluster_node.abs_path
        rpu_cluster_node_path = rpu_cluster_node_path.replace("@1", "@0")
        rpu_cluster_node_path = rpu_cluster_node_path.replace("@2", "@0")
        rpu_cluster_node_path = rpu_cluster_node_path.replace("@3", "@0")
        rpu_cluster_node = tree[rpu_cluster_node_path]

    rpu_core_node = rpu_cluster_node.abs_path + "/cpu@"
    # all cores are in cluster topologically in DTS
    rpu_core = str(int(rpu_core)) 

    # split rpu
    if  CPU_CONFIG.RPU_SPLIT == cpu_config:
        rpu_core_node = tree[rpu_core_node+rpu_core]
    elif CPU_CONFIG.RPU_LOCKSTEP:
        if rpu_core != RPU_CORE.RPU_0 or (platform == SOC_TYPE.VERSAL_NET and rpu_core == RPU_CORE.RPU_2):
            rpu_core_node = tree[rpu_core_node+rpu_core]
    else:
        print("WARNING invalid RPU and CPU config for relation. cpu: ", cpu_config, " rpu: ", rpu_core)
        return False

    if rpu_core_node.props("power-domains") == []:
        print("WARNING: RPU core does not have power-domains property.")
        return False

    rpu_core_pd_prop = rpu_core_node.props("power-domains")[0]
    channel_id = "_"+node.parent.parent.name+"_"+remote_node.name
    openamp_channel_info["elfload"+channel_id] = elfload_nodes
    openamp_channel_info["rpu_core_pd_prop"+channel_id] = rpu_core_pd_prop
    openamp_channel_info["cpu_config"+channel_id] = cpu_config
    openamp_channel_info["rpu_core"+channel_id] = rpu_core
    return True

def get_platform(tree):
    # set platform
    platform = None
    root_node = tree["/"]
    root_model = str(root_node.props("model")[0].value)

    zynqmp = [ 'zynqmp', 'zcu' ]
    versal = [ 'vck190', 'vmk180', 'vpk120', 'vpk180', 'vck5000', 'vhk158', 'xlnx,versal', 'vek280', 'versal' ]
    versalnet = [ 'versal-net', 'vc-p', 'a2197', 'Versal NET' ]
    zynq = [ 'xlnx,zynq-7000', 'zc7', 'zynq' ]

    for i in zynqmp:
        if root_model.lower() in i or i in root_model.lower():
            return SOC_TYPE.ZYNQMP
    for i in versalnet:
        if root_model.lower() in i or i in root_model.lower():
            return SOC_TYPE.VERSAL_NET
    for i in versal:
        if root_model.lower() in i or i in root_model.lower():
            return SOC_TYPE.VERSAL
    for i in zynq:
        if root_model.lower() in i or i in root_model.lower():
            return SOC_TYPE.ZYNQ

    if platform == None:
        print("Unable to find data for platform: ", root_model)

    return platform

def get_remote_node(tree, remote_nodes, index):
    return tree.pnode( remote_nodes[i] )

def populate_remote_nodes(tree, remote_prop):
    remote_nodes = []

    for remote_node in remote_prop.value:
        remote_nodes.append( tree.pnode(remote_node) )

    return remote_nodes

def xlnx_remoteproc_parse(tree, node, openamp_channel_info, verbose = 0 ):
    # Xilinx OpenAMP subroutine to collect RPMsg information from Remoteproc
    # relation
    elfload_nodes = []
    platform = get_platform(tree)
    if platform == None:
        print("Unsupported platform: ", root_compat)
        return False
    openamp_channel_info["platform"] = platform

    # check for remote property
    if node.props("remote") == []:
        print("WARNING: ", node, "is missing remote property")
        return False
    remote_nodes = populate_remote_nodes(tree, node.props("remote")[0])

    # check for elfload prop
    if node.props("elfload") == []:
        print("WARNING: ", node, " is missing elfload property")
        return False
    elfload_prop = node.props("elfload")[0]

    for i, remote_node in enumerate(remote_nodes):
        channel_elfload_nodes = []
        row_width = int(len(elfload_prop) / len(remote_nodes))
        for current_elfload in range(0,row_width):
            idx = row_width * i + current_elfload
            elfloadnode = tree.pnode( elfload_prop[idx] )
            channel_elfload_nodes.append ( elfloadnode )

        if platform in [SOC_TYPE.ZYNQMP, SOC_TYPE.VERSAL, SOC_TYPE.VERSAL_NET]:
            ret = xlnx_remoteproc_rpu_parse(tree, node, openamp_channel_info, remote_node, channel_elfload_nodes)
            if not ret:
                print("ret xlnx_remoteproc_rpu_parse false")
                return ret

        channel_id = "_"+node.parent.parent.name+"_"+remote_node.name
        openamp_channel_info["elfload"+channel_id] = channel_elfload_nodes
        ret = xlnx_remoteproc_update_tree(tree, node, remote_node, openamp_channel_info, verbose = 0 )
        if not ret:
            print("WARNING: Failed to update tree for Remoteproc.")
            return False

    return True


def xlnx_openamp_parse(sdt, options, verbose = 0 ):
    # Xilinx OpenAMP subroutine to parse OpenAMP Channel
    # information and generate Device Tree information.
    tree = sdt.tree
    ret = -1
    openamp_channel_info = {}

    for n in tree["/domains"].subnodes():
            node_compat = n.props("compatible")
            if node_compat != []:
                node_compat = node_compat[0].value

                if node_compat == REMOTEPROC_D_TO_D:
                    ret = xlnx_remoteproc_parse(tree, n, openamp_channel_info, verbose)
                elif node_compat == RPMSG_D_TO_D:
                    ret = xlnx_rpmsg_parse(tree, n, openamp_channel_info, options, verbose)

                if ret == False:
                    return ret

    return True

def xlnx_openamp_rpmsg_expand(tree, subnode, verbose = 0 ):
    # Xilinx-specific YAML expansion of RPMsg description.
    root_node = tree["/"]
    root_compat = root_node.props("compatible")[0].value
    platform = get_platform(tree)

    if platform == None:
        print("Unsupported platform: ", root_compat)
        return False

    ret = resolve_host_remote( tree, subnode, verbose)
    if ret == False:
        return ret
    ret = resolve_rpmsg_carveouts( tree, subnode, verbose)
    if ret == False:
        return ret
    ret = resolve_rpmsg_mbox( tree, subnode, verbose)
    # Zynq platform has mailboxes set in driver
    if ret == False and platform != SOC_TYPE.ZYNQ:
        return ret


    return True


def xlnx_openamp_remoteproc_expand(tree, subnode, verbose = 0 ):
    # Xilinx-specific YAML expansion of Remoteproc description.
    ret = resolve_host_remote( tree, subnode, verbose)
    if ret == False:
        return ret
    ret = resolve_remoteproc_carveouts( tree, subnode, verbose)
    if ret == False:
        return ret


    return True
