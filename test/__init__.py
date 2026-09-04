"""测试包根。

存在的唯一理由：``tests/test_gui_gateway_e2e.py`` 等文件用
``from test.unit._support import ...`` 复用夹具。没有这个文件时 ``test`` 不是包，
``pythonpath = ["."]`` 也救不了，收集期就 ``ModuleNotFoundError``。
"""
