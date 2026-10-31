from setuptools import find_packages, setup
from glob import glob
setup(name='so101_bringup', version='0.1.0', packages=find_packages(),
      data_files=[('share/ament_index/resource_index/packages', ['resource/so101_bringup']), ('share/so101_bringup', ['package.xml', 'LICENSE'])]+ [('share/so101_bringup/launch', glob('launch/*.launch.py'))], install_requires=['setuptools'], zip_safe=True,
      maintainer='SO101 developers', maintainer_email='dev@example.invalid',
      description='SO-101 Physical AI so101_bringup', license='Apache-2.0',
      entry_points={'console_scripts': ['system_diagnostics = so101_bringup.diagnostics:main']})
