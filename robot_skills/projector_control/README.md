# Projector control

`fitness_video_on` turns on the projector through `/home/test/dian.py` and starts
the fixed exercise video in the Android container. `off` calls
`dian.py::light_off()` and is safe to call repeatedly.

Install the restricted root helper once:

```bash
cd /home/test/single_function/projector_control
./install_projection_helper.sh
```

The runtime never stores or supplies a sudo password. Its sudo permission is
limited to `/usr/local/sbin/robot-start-exercise-projection`, which accepts no
arguments and uses fixed container and video paths.
