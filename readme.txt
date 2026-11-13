测试：python3 -m unittest discover -s tests/brand_mobility -p 'test_*.py'
构建：python3 -m compileall -q brand_ladder_observatory tests/brand_mobility
验收：python3 -m brand_ladder_observatory.operator.cli fixtures/demo.json --database /tmp/brand_ladder_observatory-acceptance.sqlite3
所有输入均为本地合成数据，运行不需要网络。
