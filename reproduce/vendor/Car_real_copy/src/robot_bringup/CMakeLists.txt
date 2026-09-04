cmake_minimum_required(VERSION 3.8)
project(robot_bringup)

find_package(ament_cmake REQUIRED)

install(DIRECTORY
  config
  launch
  tools
  map
  DESTINATION share/${PROJECT_NAME}
)

install(PROGRAMS
  tools/cartographer_shutdown_saver.py
  tools/save_cartographer_state.py
  tools/save_map_on_exploration_complete.py
  tools/mapping_navigation_manager.py
  tools/cleanup_robot_mission.py
  tools/wait_for_mapping_ready.py
  tools/demo/controller_cli.py
  tools/demo/visual_map_annotator.py
  DESTINATION lib/${PROJECT_NAME}
)

ament_package()
