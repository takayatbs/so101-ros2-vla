from setuptools import find_packages, setup
setup(name='so101_teleop', version='0.1.0', packages=find_packages(),
      data_files=[('share/ament_index/resource_index/packages', ['resource/so101_teleop']), ('share/so101_teleop', ['package.xml', 'LICENSE'])], install_requires=['setuptools'], zip_safe=True,
      maintainer='SO101 developers', maintainer_email='dev@example.invalid',
      description='SO-101 Physical AI so101_teleop', license='Apache-2.0',
      entry_points={'console_scripts': ['so101_teleop = so101_teleop.node:main']})
