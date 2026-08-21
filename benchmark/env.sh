#!/usr/bin/env bash
# 公共环境解析：定位可用的 Python 解释器。
#
# 用户的 Git Bash 默认没有 python3；本机可用 Kimi Work 托管 runtime 兜底。
# 优先级：$PYTHON_BIN > python3 > python > Kimi 托管 runtime。
# 都找不到时明确报错（避免 set -e 下无声退出）。

if [ -z "${PYTHON_BIN:-}" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
  else
    _KIMI_PY="$HOME/AppData/Roaming/kimi-desktop/daimon-share/daimon/runtime/python/.venv/Scripts/python3.exe"
    if [ -x "$_KIMI_PY" ]; then
      PYTHON_BIN="$_KIMI_PY"
    else
      echo "错误: 找不到 Python 解释器。请安装 Python，或设置 PYTHON_BIN 环境变量指向 python 可执行文件。" >&2
      exit 1
    fi
  fi
fi
export PYTHON_BIN
