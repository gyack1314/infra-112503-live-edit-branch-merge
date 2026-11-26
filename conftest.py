# 这个空 conftest.py 让 pytest 把仓库根目录加入 sys.path，
# 从而无论测试位于根目录还是 tests/ 下都能 `import edit_channel`。
