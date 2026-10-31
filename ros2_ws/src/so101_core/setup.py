from setuptools import find_packages, setup
setup(name='so101_core', version='0.1.0', packages=find_packages(),
      package_data={'so101_core': ['review_assets/*']},
      data_files=[('share/ament_index/resource_index/packages', ['resource/so101_core']), ('share/so101_core', ['package.xml', 'LICENSE', 'NOTICE']), ('share/so101_core/LICENSES', [
          'LICENSES/LeRobot-LICENSE.txt', 'LICENSES/FTServo-LICENSE.txt',
          'LICENSES/GR00T-N1.7-model-LICENSE.txt'])], install_requires=['setuptools'], zip_safe=True,
      maintainer='SO101 developers', maintainer_email='dev@example.invalid',
      description='SO-101 Physical AI so101_core', license='Apache-2.0',
      entry_points={'console_scripts': []})
