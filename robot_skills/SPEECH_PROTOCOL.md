# single_function Speech Protocol

`single_function` skills do not own the speaker directly.  They emit speech-worthy progress to stdout, and `/home/test/new_project` owns the actual TTS and speaker hardware.

This keeps hardware resources centralized:

- `single_function/<skill>/run.sh` runs the skill and writes normal logs plus human progress lines.
- `/home/test/new_project/new_project.executor.SkillExecutor` streams stdout in real time.
- `/home/test/new_project/new_project.speech.SkillSpeechRouter` filters speech-worthy lines.
- `/home/test/new_project/new_project.audio.AudioManager` performs actual TTS playback.

Runtime environment expected by the executor:

```bash
PYTHONUNBUFFERED=1
PYTHONIOENCODING=utf-8
SINGLE_FUNCTION_JSON=1
SINGLE_FUNCTION_SPEECH_EVENTS=1
```

Recommended stdout speech lines:

- Start: `深蹲计数已开始，时长三十秒。`
- Progress: `第一个`
- Completion: `三十秒已到，本次深蹲计数结束，共做了三个。`
- Not found: `抱歉，我没有发现小狗`
- Status: `您目前做了 3 个俯卧撑了`

Final machine-readable JSON should still be printed as the last JSON object.  The JSON is stored in TaskGroup history; speech lines are spoken immediately.
