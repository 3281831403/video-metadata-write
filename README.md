# video-metadata-write

## 题面

批量写入标题和备注后，文件体积暴涨、部分视频还打不开了：box size 不含头部 8 字节，整体错位；64 位扩展 size 未处理，大文件解析失败；替换后不修正父 box size，播放器读到垃圾；直接覆盖原文件，写一半断电即损坏；UTF-8 长度用字符数而不是字节数，中文截断；删除元数据时连带删除 moov，文件不可播；box 越界不校验，越界读；类型非 ASCII 不报错，写入非法 box；批量中途失败留下部分写入；size=0 不校验，解析死循环。meta/writer.py 是核心，tests/test_writer.py 15个红。要修的是：修复上述 10 个缺陷，让 15 条测试全部通过。

## 运行方式

在仓库根目录执行：

```powershell
python -m pytest tests -q
```

初始状态必须恰好是 `15 failed`，且没有任何 collection error、import error 或 syntax error。修复 `meta/writer.py` 后，目标是 `15 passed`。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 文件

```text
video-metadata-write/
├── README.md
├── conftest.py
├── .gitignore
├── meta/
│   ├── __init__.py
│   └── writer.py
└── tests/
    └── test_writer.py
```

## 约束

- 只用标准库（`struct`、`json`）与 pytest，离线运行，不使用随机数。
- `tests/` 不可修改；实现只改 `meta/writer.py`。
- `meta/writer.py` 的 8 个函数目前全是 `raise NotImplementedError`，docstring 即验收契约：原子替换、MP4 box 结构、1 字节容差。
- 不执行 `git init`，不联网。
