from setuptools import find_packages, setup
setup(name='so101_policy', version='0.1.0', packages=find_packages(),
      data_files=[('share/ament_index/resource_index/packages', ['resource/so101_policy']), ('share/so101_policy', ['package.xml', 'LICENSE'])], install_requires=['setuptools'], zip_safe=True,
      maintainer='SO101 developers', maintainer_email='dev@example.invalid',
      description='SO-101 Physical AI so101_policy', license='Apache-2.0',
      entry_points={'console_scripts': ['policy_node = so101_policy.node:main',
                                       'benchmark_policy_node = so101_policy.benchmark_node:main']})
