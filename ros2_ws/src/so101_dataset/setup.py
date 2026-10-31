from setuptools import find_packages, setup
setup(name='so101_dataset', version='0.1.0', packages=find_packages(),
      data_files=[('share/ament_index/resource_index/packages', ['resource/so101_dataset']), ('share/so101_dataset', ['package.xml', 'LICENSE'])], install_requires=['setuptools'], zip_safe=True,
      maintainer='SO101 developers', maintainer_email='dev@example.invalid',
      description='SO-101 Physical AI so101_dataset', license='Apache-2.0',
      entry_points={'console_scripts': ['episode_manager = so101_dataset.episode:main', 'data_recorder = so101_dataset.recorder:main', 'convert_dataset = so101_core.converter:main']})
