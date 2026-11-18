# 实验脚本接口
`file_path=skill://experiments/scripts/experiment.py`，`args=["--input", "input.json", "--base-dir", "."]`

`{"path":"experiment.py","output":"experiment-receipt.json","arguments":[],"timeout":60}`
脚本必须已存在于项目，参数为字符串列表。执行受同一run_code白名单约束；输出记录脚本SHA256、环境、参数、开始/结束、日志及completed/failed。非零退出或超时传播为失败，仍保存已有失败回执。文本/图片实验日志由模型按methods.md整理，非此运行脚本自动识别。
