"""成片中心（阶段 D）：时间线、前置检查、FFmpeg 执行与产物验收。

这里不建第二个队列——合成仍是一条普通 Job（provider=ffmpeg），
由同一个 worker 领取，日志与产物落在同一个 data/jobs/<id>/ 下。
"""
