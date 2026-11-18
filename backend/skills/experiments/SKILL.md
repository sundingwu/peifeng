---
name: experiments
title: 实验与复现
description: 执行Python实验、记录样品与异常，复现论文方法和基线。
---

# 实验与复现

区分真正运行程序与整理现场材料。运行保留实际状态、参数和来源；观察、解释与计划分开，不编造实验、转录或设备信息。

## 按需读取

- references/methods.md：复现实验、图片/文字日志、样品设备和异常。
- references/script-interface.md：执行已有项目脚本并保存回执。

执行入口：`file_path=skill://experiments/scripts/experiment.py`，`args=["--input", "input.json", "--base-dir", "."]`。输入为单对象、数组或targets；参数见上方接口，检查每项真实结果。
