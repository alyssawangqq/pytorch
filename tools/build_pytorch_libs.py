from .setup_helpers.env import (IS_64BIT, IS_DARWIN, IS_WINDOWS,
                                DEBUG, REL_WITH_DEB_INFO, USE_MKLDNN,
                                check_env_flag, check_negative_env_flag)

import os
import sys
import distutils
import distutils.sysconfig
from subprocess import check_call, check_output
from distutils.version import LooseVersion
from .setup_helpers.cuda import USE_CUDA, CUDA_HOME
from .setup_helpers.dist_check import USE_DISTRIBUTED, USE_GLOO_IBVERBS
from .setup_helpers.nccl import USE_SYSTEM_NCCL, NCCL_INCLUDE_DIR, NCCL_ROOT_DIR, NCCL_SYSTEM_LIB, USE_NCCL
from .setup_helpers.rocm import USE_ROCM
from .setup_helpers.nnpack import USE_NNPACK
from .setup_helpers.qnnpack import USE_QNNPACK
from .setup_helpers.cudnn import CUDNN_INCLUDE_DIR, CUDNN_LIBRARY, USE_CUDNN


from pprint import pprint
from glob import glob
import multiprocessing
import shutil


def which(thefile):
    path = os.environ.get("PATH", os.defpath).split(os.pathsep)
    for dir in path:
        fname = os.path.join(dir, thefile)
        fnames = [fname]
        if IS_WINDOWS:
            exts = os.environ.get('PATHEXT', '').split(os.pathsep)
            fnames += [fname + ext for ext in exts]
        for name in fnames:
            if (os.path.exists(name) and os.access(name, os.F_OK | os.X_OK)
                    and not os.path.isdir(name)):
                return name
    return None


def cmake_version(cmd):
    for line in check_output([cmd, '--version']).decode('utf-8').split('\n'):
        if 'version' in line:
            return LooseVersion(line.strip().split(' ')[2])
    raise Exception('no version found')


def get_cmake_command():
    cmake_command = 'cmake'
    if IS_WINDOWS:
        return cmake_command
    cmake3 = which('cmake3')
    if cmake3 is not None:
        cmake = which('cmake')
        if cmake is not None:
            bare_version = cmake_version(cmake)
            if bare_version < LooseVersion("3.5.0") and cmake_version(cmake3) > bare_version:
                cmake_command = 'cmake3'
    return cmake_command


def cmake_defines(lst, **kwargs):
    for key in sorted(kwargs.keys()):
        value = kwargs[key]
        if value is not None:
            lst.append('-D{}={}'.format(key, value))


# Ninja
# Use ninja if it is on the PATH. Previous version of PyTorch required the
# ninja python package, but we no longer use it, so we do not have to import it
USE_NINJA = not check_negative_env_flag('USE_NINJA') and (which('ninja') is not None)

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
install_dir = base_dir + "/torch"
build_type = "Release"
if DEBUG:
    build_type = "Debug"
elif REL_WITH_DEB_INFO:
    build_type = "RelWithDebInfo"


def overlay_windows_vcvars(env):
    if sys.version_info >= (3, 5):
        from distutils._msvccompiler import _get_vc_env
        vc_arch = 'x64' if IS_64BIT else 'x86'
        vc_env = _get_vc_env(vc_arch)
        # Keys in `_get_vc_env` are always lowercase.
        # We turn them into uppercase before overlaying vcvars
        # because OS environ keys are always uppercase on Windows.
        # https://stackoverflow.com/a/7797329
        vc_env = {k.upper(): v for k, v in vc_env.items()}
        for k, v in env.items():
            uk = k.upper()
            if uk not in vc_env:
                vc_env[uk] = v
        return vc_env
    else:
        return env


def mkdir_p(dir):
    try:
        os.makedirs(dir)
    except OSError:
        pass


def create_build_env():
    # XXX - our cmake file sometimes looks at the system environment
    # and not cmake flags!
    # you should NEVER add something to this list. It is bad practice to
    # have cmake read the environment
    my_env = os.environ.copy()
    if USE_CUDNN:
        my_env['CUDNN_LIBRARY'] = _escape_path(CUDNN_LIBRARY)
        my_env['CUDNN_INCLUDE_DIR'] = _escape_path(CUDNN_INCLUDE_DIR)
    if USE_CUDA:
        my_env['CUDA_BIN_PATH'] = _escape_path(CUDA_HOME)

    if IS_WINDOWS and USE_NINJA:
        # When using Ninja under Windows, the gcc toolchain will be chosen as
        # default. But it should be set to MSVC as the user's first choice.
        my_env = overlay_windows_vcvars(my_env)
        my_env.setdefault('CC', 'cl')
        my_env.setdefault('CXX', 'cl')
    return my_env


def run_cmake(version,
              cmake_python_library,
              build_python,
              build_test,
              build_dir,
              my_env):
    cmake_args = [
        get_cmake_command()
    ]
    if USE_NINJA:
        cmake_args.append('-GNinja')
    elif IS_WINDOWS:
        cmake_args.append('-GVisual Studio 15 2017')
        if IS_64BIT:
            cmake_args.append('-Ax64')
            cmake_args.append('-Thost=x64')
    try:
        import numpy as np
    except ImportError:
        USE_NUMPY = False
        NUMPY_INCLUDE_DIR = None
    else:
        NUMPY_INCLUDE_DIR = np.get_include()
        USE_NUMPY = True

    cflags = os.getenv('CFLAGS', "") + " " + os.getenv('CPPFLAGS', "")
    ldflags = os.getenv('LDFLAGS', "")
    if IS_WINDOWS:
        cmake_defines(cmake_args, MSVC_Z7_OVERRIDE=os.getenv('MSVC_Z7_OVERRIDE', "ON"))
        cflags += " /EHa"

    mkdir_p(install_dir)
    mkdir_p(build_dir)

    cmake_defines(
        cmake_args,
        PYTHON_EXECUTABLE=_escape_path(sys.executable),
        PYTHON_LIBRARY=_escape_path(cmake_python_library),
        PYTHON_INCLUDE_DIR=_escape_path(distutils.sysconfig.get_python_inc()),
        BUILDING_WITH_TORCH_LIBS=os.getenv("BUILDING_WITH_TORCH_LIBS", "ON"),
        TORCH_BUILD_VERSION=version,
        CMAKE_BUILD_TYPE=build_type,
        BUILD_PYTHON=build_python,
        BUILD_SHARED_LIBS=os.getenv("BUILD_SHARED_LIBS", "ON"),
        BUILD_BINARY=check_env_flag('BUILD_BINARY'),
        BUILD_TEST=build_test,
        INSTALL_TEST=build_test,
        BUILD_CAFFE2_OPS=not check_negative_env_flag('BUILD_CAFFE2_OPS'),
        ONNX_NAMESPACE=os.getenv("ONNX_NAMESPACE", "onnx_torch"),
        ONNX_ML=os.getenv("ONNX_ML", False),
        USE_CUDA=USE_CUDA,
        USE_DISTRIBUTED=USE_DISTRIBUTED,
        USE_FBGEMM=not (check_env_flag('NO_FBGEMM') or check_negative_env_flag('USE_FBGEMM')),
        NAMEDTENSOR_ENABLED=(check_env_flag('USE_NAMEDTENSOR') or check_negative_env_flag('NO_NAMEDTENSOR')),
        USE_NUMPY=USE_NUMPY,
        NUMPY_INCLUDE_DIR=_escape_path(NUMPY_INCLUDE_DIR),
        USE_SYSTEM_NCCL=USE_SYSTEM_NCCL,
        NCCL_INCLUDE_DIR=NCCL_INCLUDE_DIR,
        NCCL_ROOT_DIR=NCCL_ROOT_DIR,
        NCCL_SYSTEM_LIB=NCCL_SYSTEM_LIB,
        CAFFE2_STATIC_LINK_CUDA=check_env_flag('USE_CUDA_STATIC_LINK'),
        USE_ROCM=USE_ROCM,
        USE_NNPACK=USE_NNPACK,
        USE_LEVELDB=check_env_flag('USE_LEVELDB'),
        USE_LMDB=check_env_flag('USE_LMDB'),
        USE_OPENCV=check_env_flag('USE_OPENCV'),
        USE_QNNPACK=USE_QNNPACK,
        USE_TENSORRT=check_env_flag('USE_TENSORRT'),
        USE_FFMPEG=check_env_flag('USE_FFMPEG'),
        USE_SYSTEM_EIGEN_INSTALL="OFF",
        USE_MKLDNN=USE_MKLDNN,
        USE_NCCL=USE_NCCL,
        NCCL_EXTERNAL=USE_NCCL,
        CMAKE_INSTALL_PREFIX=install_dir,
        CMAKE_C_FLAGS=cflags,
        CMAKE_CXX_FLAGS=cflags,
        CMAKE_EXE_LINKER_FLAGS=ldflags,
        CMAKE_SHARED_LINKER_FLAGS=ldflags,
        THD_SO_VERSION="1",
        CMAKE_PREFIX_PATH=os.getenv('CMAKE_PREFIX_PATH') or distutils.sysconfig.get_python_lib(),
        BLAS=os.getenv('BLAS'),
        CUDA_NVCC_EXECUTABLE=_escape_path(os.getenv('CUDA_NVCC_EXECUTABLE')),
        USE_REDIS=os.getenv('USE_REDIS'),
        USE_GLOG=os.getenv('USE_GLOG'),
        USE_GFLAGS=os.getenv('USE_GFLAGS'),
        USE_ASAN=check_env_flag('USE_ASAN'),
        WERROR=os.getenv('WERROR'))

    if os.getenv('_GLIBCXX_USE_CXX11_ABI'):
        cmake_defines(cmake_args, GLIBCXX_USE_CXX11_ABI=os.getenv('_GLIBCXX_USE_CXX11_ABI'))

    if os.getenv('USE_OPENMP'):
        cmake_defines(cmake_args, USE_OPENMP=check_env_flag('USE_OPENMP'))

    if os.getenv('USE_TBB'):
        cmake_defines(cmake_args, USE_TBB=check_env_flag('USE_TBB'))

    if os.getenv('MKL_SEQ'):
        cmake_defines(cmake_args, INTEL_MKL_SEQUENTIAL=check_env_flag('MKL_SEQ'))

    if os.getenv('MKL_TBB'):
        cmake_defines(cmake_args, INTEL_MKL_TBB=check_env_flag('MKL_TBB'))

    mkldnn_threading = os.getenv('MKLDNN_THREADING')
    if mkldnn_threading:
        cmake_defines(cmake_args, MKLDNN_THREADING=mkldnn_threading)

    parallel_backend = os.getenv('PARALLEL_BACKEND')
    if parallel_backend:
        cmake_defines(cmake_args, PARALLEL_BACKEND=parallel_backend)

    if USE_GLOO_IBVERBS:
        cmake_defines(cmake_args, USE_IBVERBS="1", USE_GLOO_IBVERBS="1")

    if USE_MKLDNN:
        cmake_defines(cmake_args, MKLDNN_ENABLE_CONCURRENT_EXEC="ON")

    expected_wrapper = '/usr/local/opt/ccache/libexec'
    if IS_DARWIN and os.path.exists(expected_wrapper):
        cmake_defines(cmake_args,
                      CMAKE_C_COMPILER="{}/gcc".format(expected_wrapper),
                      CMAKE_CXX_COMPILER="{}/g++".format(expected_wrapper))
    for env_var_name in my_env:
        if env_var_name.startswith('gh'):
            # github env vars use utf-8, on windows, non-ascii code may
            # cause problem, so encode first
            try:
                my_env[env_var_name] = str(my_env[env_var_name].encode("utf-8"))
            except UnicodeDecodeError as e:
                shex = ':'.join('{:02x}'.format(ord(c)) for c in my_env[env_var_name])
                sys.stderr.write('Invalid ENV[{}] = {}\n'.format(env_var_name, shex))
    # According to the CMake manual, we should pass the arguments first,
    # and put the directory as the last element. Otherwise, these flags
    # may not be passed correctly.
    # Reference:
    # 1. https://cmake.org/cmake/help/latest/manual/cmake.1.html#synopsis
    # 2. https://stackoverflow.com/a/27169347
    cmake_args.append(base_dir)
    pprint(cmake_args)
    check_call(cmake_args, cwd=build_dir, env=my_env)


def build_caffe2(version,
                 cmake_python_library,
                 build_python,
                 rerun_cmake,
                 cmake_only,
                 build_dir):
    my_env = create_build_env()
    build_test = not check_negative_env_flag('BUILD_TEST')
    max_jobs = os.getenv('MAX_JOBS', str(multiprocessing.cpu_count()))
    cmake_cache_file = os.path.join(build_dir, 'CMakeCache.txt')
    ninja_build_file = os.path.join(build_dir, 'build.ninja')
    if rerun_cmake and os.path.isfile(cmake_cache_file):
        os.remove(cmake_cache_file)
    if not os.path.exists(cmake_cache_file) or (
            USE_NINJA and not os.path.exists(ninja_build_file)):
        run_cmake(version,
                  cmake_python_library,
                  build_python,
                  build_test,
                  build_dir,
                  my_env)
    if cmake_only:
        return
    build_cmd = [get_cmake_command(),
                 '--build', '.', '--target', 'install', '--config', build_type]
    # This ``if-else'' clause would be unnecessary when cmake 3.12 becomes
    # minimum, which provides a '-j' option: build_cmd += ['-j', max_jobs]
    # would be sufficient by then.
    if IS_WINDOWS and not USE_NINJA:  # We are likely using msbuild here
        build_cmd += ['--', '/maxcpucount:{}'.format(max_jobs)]
    else:
        build_cmd += ['--', '-j', max_jobs]
    check_call(build_cmd, cwd=build_dir, env=my_env)

    # in cmake, .cu compilation involves generating certain intermediates
    # such as .cu.o and .cu.depend, and these intermediates finally get compiled
    # into the final .so.
    # Ninja updates build.ninja's timestamp after all dependent files have been built,
    # and re-kicks cmake on incremental builds if any of the dependent files
    # have a timestamp newer than build.ninja's timestamp.
    # There is a cmake bug with the Ninja backend, where the .cu.depend files
    # are still compiling by the time the build.ninja timestamp is updated,
    # so the .cu.depend file's newer timestamp is screwing with ninja's incremental
    # build detector.
    # This line works around that bug by manually updating the build.ninja timestamp
    # after the entire build is finished.
    if os.path.exists(ninja_build_file):
        os.utime(ninja_build_file, None)

    if build_python:
        caffe2_proto_dir = os.path.join(build_dir, 'caffe2', 'proto')
        for proto_file in glob(os.path.join(caffe2_proto_dir, '*.py')):
            if proto_file != os.path.join(caffe2_proto_dir, '__init__.py'):
                shutil.copy(proto_file, os.path.join('caffe2', 'proto'))


def _escape_path(path):
    if os.path.sep != '/' and path is not None:
        return path.replace(os.path.sep, '/')
    return path
