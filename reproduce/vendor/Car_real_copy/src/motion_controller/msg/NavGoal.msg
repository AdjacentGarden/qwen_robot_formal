# map 坐标系导航目标。x/y 单位 m；yaw 单位 degree，允许范围 [-180, 180]。
float64 x
float64 y
float64 yaw

# false：Nav2 成功即完成；true：Nav2 成功后自动执行墙面对齐。
bool align_to_wall
