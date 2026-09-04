cmake_minimum_required(VERSION 3.8)
project(robot_system_test)

find_package(ament_cmake REQUIRED)

install(PROGRAMS
  tools/robot_graph_test.py
  tools/motion_controller_auto_test.py
  tools/motion_controller_live_auto_test.py
  tools/lidar_diagnostic_test.py
  tools/rplidar_c1_serial_diagnostic.py
  DESTINATION lib/${PROJECT_NAME}
)
install(PROGRAMS robot_system_test/hardware_stability_test.py
  DESTINATION lib/${PROJECT_NAME}
  RENAME hardware_stability_test
)
install(DIRECTORY robot_system_test
  DESTINATION lib/${PROJECT_NAME}
  PATTERN "__pycache__" EXCLUDE
)
install(DIRECTORY config launch DESTINATION share/${PROJECT_NAME})
install(FILES README.md DESTINATION share/${PROJECT_NAME})

ament_package()
