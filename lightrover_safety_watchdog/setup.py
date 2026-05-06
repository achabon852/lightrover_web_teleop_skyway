from setuptools import setup
package_name = 'lightrover_safety_watchdog'
setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[('share/ament_index/resource_index/packages', ['resource/' + package_name]),('share/' + package_name, ['package.xml'])],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Lightrover',
    maintainer_email='user@example.com',
    description='Lightweight cmd_vel safety watchdog for Lightrover',
    license='Apache-2.0',
    entry_points={'console_scripts': ['safety_watchdog = lightrover_safety_watchdog.safety_watchdog:main']},
)
