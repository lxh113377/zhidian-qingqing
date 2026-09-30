$ErrorActionPreference = 'Stop'
$python = 'python'  # 按本机实际解释器路径替换
$script = Join-Path $PSScriptRoot 'one_click_deploy.py'
& $python $script @args
exit $LASTEXITCODE
