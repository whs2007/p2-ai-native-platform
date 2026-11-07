# 开发环境准备（Windows PowerShell）：检查 Python、安装 pytest、跑全量测试
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[1/4] Python 版本"
python --version
if ($LASTEXITCODE -ne 0) { throw "未找到 python，请先安装 Python 3.11+" }

Write-Host "[2/4] 安装开发依赖（可选，本机已有 pytest 可跳过）"
python -c "import pytest" 2>$null
if ($LASTEXITCODE -ne 0) {
    python -m pip install -e ".[dev]"
} else {
    Write-Host "  pytest 已安装"
}

Write-Host "[3/4] 全量测试"
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "测试失败" }

Write-Host "[4/4] demo 环境自检"
python scripts/demo.py --mode mock --reset

Write-Host "完成。演示：python scripts/demo.py --mode live"