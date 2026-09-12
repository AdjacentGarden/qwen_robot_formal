import importlib.util
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def test_service_defaults_hybrid_and_offers_local_only():
    spec=importlib.util.spec_from_file_location('app_service',ROOT/'scripts/app_service.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert '--hybrid-model' in module.service_command()
    command=module.service_command('local')
    assert '--local-stack' in command and '--cloud-model' not in command
    assert command[command.index('--model')+1]=='Qwen3-4B'
    assert '--cloud-model' in module.service_command('cloud')


def test_local_stack_rejects_remote_model(tmp_path):
    env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
    result=subprocess.run([sys.executable,'-m','robot_graph.cli','--state-dir',str(tmp_path),'--local-stack','--model-url','https://example.invalid/v1','turn','你好'],capture_output=True,text=True,env=env)
    assert result.returncode==2
    assert 'requires a loopback' in result.stderr
    assert not list(tmp_path.iterdir())
