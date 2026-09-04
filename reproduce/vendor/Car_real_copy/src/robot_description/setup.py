from glob import glob
from setuptools import setup, find_packages
import os

package_name = 'robot_description'

def get_data_files(directory):
    """获取目录下的所有文件，并保持目录结构"""
    data_files = []
    if os.path.exists(directory):
        for root, dirs, files in os.walk(directory):
            # 计算安装路径（去掉 directory 前缀）
            install_dir = os.path.join('share', package_name, root)
            # 获取该目录下的所有文件
            file_paths = [os.path.join(root, f) for f in files]
            if file_paths:
                data_files.append((install_dir, file_paths))
    return data_files

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ] + get_data_files('urdf') + get_data_files('sdf') + get_data_files('meshes') + get_data_files('world') + get_data_files('launch') + get_data_files('rviz'),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='mr-cheng',
    maintainer_email='1959711225@qq.com',
    description='Robot description package for simulation',
    license='Apache License 2.0',
    entry_points={
        'console_scripts': [
            'imu_covariance_relay = robot_description.imu_covariance_relay:main',
        ],
    },
)