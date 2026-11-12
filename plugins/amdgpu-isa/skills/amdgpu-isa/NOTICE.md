Instruction data derives from AMD's machine-readable GPU ISA
specification (<https://gpuopen.com/machine-readable-isa/>), which each file
declares as:

    Copyright (c) 2026 Advanced Micro Devices, Inc., or its affiliates.
    Sensitivity: AMD Public Use.
    License: MIT

MIT permission notice, as required for redistribution:

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

LLVM gfx target mappings
------------------------

The gfx_target table and v_gfx view derive from the AMDGPU Processors table in
LLVM's llvm/docs/AMDGPUUsage.rst, pinned to commit
4ee9dafceb16232b5c06857551228ee5471dc5cd:

https://github.com/llvm/llvm-project/blob/4ee9dafceb16232b5c06857551228ee5471dc5cd/llvm/docs/AMDGPUUsage.rst

This package extracts and normalizes that table and applies product-architecture
overrides to LLVM's encoding-generation headings. These mappings are distributed
under the Apache License 2.0 with LLVM Exceptions. The full license and retained
LLVM notices are included in LICENSE.LLVM.TXT.

Query script and skill text
---------------------------

Copyright (c) 2026 Li Jinpei. Distributed under the MIT License; see LICENSE.
