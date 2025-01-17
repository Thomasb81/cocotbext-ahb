#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File              : ahb_slave.py
# License           : MIT license <Check LICENSE>
# Author            : Anderson I. da Silva (aignacio) <anderson@aignacio.com>
# Date              : 16.10.2023
# Last Modified Date: 13.11.2023

import cocotb
import logging
import random
import copy

from .ahb_types import AHBTrans, AHBWrite, AHBSize, AHBResp
from .ahb_bus import AHBBus
from .version import __version__

from cocotb.triggers import RisingEdge
from cocotb.types import LogicArray
from cocotb.binary import BinaryValue
from typing import Optional, Union, Generator, List
from .memory import Memory


class AHBLiteSlave:
    def __init__(
        self,
        bus: AHBBus,
        clock: str,
        reset: str,
        def_val: Union[int, str] = "Z",
        bp: Generator[int, None, None] = None,
        name: str = "ahb_lite",
        **kwargs,
    ):
        self.bus = bus
        self.clk = clock
        self.rst = reset
        self.def_val = def_val
        self.bp = bp
        self.log = logging.getLogger(
            f"cocotb.{name}.{bus._name}." f"{bus._entity._name}"
        )
        self._init_bus()
        self.log.info(f"AHB ({name}) slave")
        self.log.info("cocotbext-ahb version %s", __version__)
        self.log.info("Copyright (c) 2023 Anderson Ignacio da Silva")
        self.log.info("https://github.com/aignacio/cocotbext-ahb")

        cocotb.start_soon(self._proc_txn())

    def _init_bus(self) -> None:
        """Initialize the bus with default value."""
        for signal in self.bus._signals:
            if signal in ["hready", "hresp", "hrdata"]:
                sig = getattr(self.bus, signal)
                try:
                    default_value = self._get_def(len(sig))
                    sig.setimmediatevalue(default_value)
                except AttributeError:
                    pass

    def _get_def(self, width: int = 1) -> BinaryValue:
        """Return a handle obj with the default value"""
        return LogicArray([self.def_val for _ in range(width)])

    async def _proc_txn(self):
        """Process any incoming txns"""

        wr_start = False
        rd_start = False
        error = False
        txn_addr = 0
        txn_size = AHBSize.WORD
        txn_type = AHBWrite.READ

        self.bus.hrdata.value = self._get_def(len(self.bus.hrdata))
        while True:
            # Wait for a txn
            await RisingEdge(self.clk)

            cur_hready = copy.deepcopy(self.bus.hready.value)
            cur_hresp = copy.deepcopy(self.bus.hresp.value)

            # Default values in case there is no txn
            self.bus.hready.value = self._get_def(1)
            self.bus.hresp.value = AHBResp.OKAY

            if self.bp is not None:
                ready = next(self.bp)
            else:
                ready = True

            if ready:
                self.bus.hready.value = 1

            if error and (cur_hresp == AHBResp.OKAY):  # First cycle of error response
                self.bus.hready.value = 0
                self.bus.hresp.value = AHBResp.ERROR
            elif error and (
                cur_hresp == AHBResp.ERROR
            ):  # Second cycle of error response
                self.bus.hready.value = 1
                self.bus.hresp.value = AHBResp.ERROR
                error = False
            else:
                if rd_start and cur_hready:
                    rd_start = False
                    self.bus.hrdata.value = self._get_def(len(self.bus.hrdata))

                if wr_start and cur_hready:
                    wr_start = False
                    if txn_type == AHBWrite.WRITE:
                        wr = self._wr(txn_addr, txn_size, self.bus.hwdata.value)
                        self.bus.hrdata.value = wr
                        self.bus.hresp.value = AHBResp.OKAY

            # Check for new txn
            if (
                (cur_hready == 1)
                and self._check_inputs()
                and self._check_valid_txn()
            ):
                txn_addr = self.bus.haddr.value
                txn_size = AHBSize(self.bus.hsize.value)
                txn_type = AHBWrite(self.bus.hwrite.value)
                self._check_size(2**txn_size, self.bus.data_width)

                if txn_type == AHBWrite.WRITE:
                    if not self._chk_wr(txn_addr, txn_size):
                        self.bus.hready.value = 0
                        self.bus.hrdata.value = 0
                        error = True
                        wr_start = False
                    else:
                        wr_start = True
                        self.bus.hresp.value = AHBResp.OKAY

                if txn_type == AHBWrite.READ:
                    if not self._chk_rd(txn_addr, txn_size):
                        self.bus.hready.value = 0
                        error = True
                    else:
                        rd_start = True
                        self.bus.hrdata.value = self._rd(txn_addr, txn_size)
                        self.bus.hresp.value = AHBResp.OKAY

    @staticmethod
    def _check_size(size: int, data_bus_width: int) -> None:
        """Check that the provided transfer size is valid."""
        if size > data_bus_width:
            raise ValueError(
                "Size of the transfer ({} B)"
                " provided is larger than the bus width "
                "({} B)".format(size, data_bus_width)
            )
        elif size <= 0 or (size & (size - 1)) != 0:
            raise ValueError(f"Error -> {size} - Size must" f"be a positive power of 2")

    def _check_inputs(self) -> bool:
        """Check any of the master signals are resolvable (i.e not 'z')"""
        signals = {
            "htrans": self.bus.htrans,
            "hwrite": self.bus.hwrite,
            "haddr": self.bus.haddr,
            "hsize": self.bus.hsize,
        }

        for var, val in signals.items():
            if val.value.is_resolvable is False:
                # self.log.warn(f"{var} is not resolvable")
                return False
        return True

    def _check_valid_txn(self) -> bool:
        htrans_st = (AHBTrans(self.bus.htrans.value) != AHBTrans.IDLE) and (
            AHBTrans(self.bus.htrans.value) != AHBTrans.BUSY
        )

        if self.bus.hsel_exist:
            if self.bus.hready_in_exist:
                if (
                    (self.bus.hsel.value == 1)
                    and (self.bus.hready_in.value == 1)
                    and htrans_st
                ):
                    return True
                else:
                    return False
            else:
                if (self.bus.hsel.value == 1) and htrans_st:
                    return True
                else:
                    return False
        else:
            if htrans_st:
                return True
            else:
                return False

    def _default_generator(self) -> Generator[bool, None, None]:
        while True:
            yield True

    def _chk_rd(self, addr: int, size: AHBSize) -> bool:
        return True

    def _chk_wr(self, addr: int, size: AHBSize) -> bool:
        return True

    def _rd(self, addr: int, size: AHBSize) -> int:
        # Return some random data when read
        return 0

    def _wr(self, addr: int, size: AHBSize, value: int) -> int:
        # Return some zero data when write
        return 0


class AHBLiteSlaveRAM(AHBLiteSlave):
    def __init__(
        self,
        bus: AHBBus,
        clock: str,
        reset: str,
        def_val: Union[int, str] = "Z",
        bp: Generator[int, None, None] = None,
        name: str = "ahb_lite_ram",
        mem_size: int = 1024,
        **kwargs,
    ):
        super().__init__(bus, clock, reset, def_val, bp, name, **kwargs)
        self.memory = Memory(size=mem_size)

    def _chk_rd(self, addr: int, size: AHBSize) -> bool:
        if addr + (2**size) > self.memory.size:
            return False
        return True

    def _chk_wr(self, addr: int, size: AHBSize) -> bool:
        if addr + (2**size) > self.memory.size:
            return False
        return True

    def _rd(self, addr: int, size: AHBSize) -> int:
        data = self.memory.read(addr, 2**size)
        data = int.from_bytes(data, byteorder="little")
        return data

    def _wr(self, addr: int, size: AHBSize, value: BinaryValue) -> int:
        if size == AHBSize.BYTE:
            data = value & 0xFF
        elif size == AHBSize.HWORD:
            data = value & 0xFFFF
        elif size == AHBSize.WORD:
            data = value & 0xFFFFFFFF
        elif size == AHBSize.DWORD:
            data = value & 0xFFFFFFFFFFFFFFFF

        data = data.to_bytes(2**size, byteorder="little")

        self.memory.write(addr, data)
        return 0


class AHBSlave(AHBLiteSlave):
    def __init__(
        self,
        bus: AHBBus,
        clock: str,
        reset: str,
        def_val: Union[int, str] = "Z",
        bp: Generator[int, None, None] = None,
        name: str = "ahb_slave",
        **kwargs,
    ):
        super().__init__(bus, clock, reset, def_val, bp, name, **kwargs)
