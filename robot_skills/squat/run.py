#!/usr/bin/env python3
SKILL_NAME = 'squat'

# ===== Unified single-function CLI preflight =====
def _single_function_cli_preflight(skill_name):
    import json as _json, os as _os, sys as _sys, time as _time
    raw = list(_sys.argv[1:])
    dry_run = False
    json_mode = False
    timeout = None
    kept = [_sys.argv[0]]
    i = 0
    while i < len(raw):
        arg = raw[i]
        if arg == '--dry-run':
            dry_run = True
            i += 1
            continue
        if arg == '--json':
            json_mode = True
            i += 1
            continue
        if arg == '--timeout':
            if i + 1 < len(raw):
                timeout = raw[i + 1]
                i += 2
            else:
                i += 1
            continue
        if arg.startswith('--timeout='):
            timeout = arg.split('=', 1)[1]
            i += 1
            continue
        kept.append(arg)
        i += 1
    _sys.argv[:] = kept
    if json_mode:
        _os.environ['SINGLE_FUNCTION_JSON'] = '1'
    if timeout is not None:
        _os.environ['SINGLE_FUNCTION_TIMEOUT'] = str(timeout)
    if dry_run:
        action = 'default'
        for token in kept[1:]:
            if not token.startswith('-'):
                action = token
                break
        print(_json.dumps({
            'ok': True,
            'status': 'dry_run',
            'skill': skill_name,
            'action': action,
            'result': {'argv': kept[1:], 'timeout': timeout},
            'error': None,
            'metrics': {'ts': round(_time.time(), 3)},
        }, ensure_ascii=False))
        raise SystemExit(0)

_single_function_cli_preflight(SKILL_NAME)
# ===== End unified single-function CLI preflight =====


import os
import sys
import types
from pathlib import Path
SKILL_DIR = Path(__file__).resolve().parent
SINGLE_FUNCTION_ROOT = SKILL_DIR
ASSETS_DIR = SKILL_DIR / 'assets'
RUNTIME_DIR = SKILL_DIR / 'runtime'
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('SINGLE_FUNCTION_ROOT', str(SINGLE_FUNCTION_ROOT))
os.environ.setdefault('SINGLE_FUNCTION_RUNTIME_DIR', str(RUNTIME_DIR))
os.environ.setdefault('FITNESS_SAMPLES_DIR', str(ASSETS_DIR / 'fitness_poses_csvs_out'))
os.environ.setdefault('FITNESS_FONT_PATH', str(ASSETS_DIR / 'movement_count_2' / 'Roboto-Regular.ttf'))
def _project_back_camera_default():
    explicit = os.getenv('FITNESS_CAMERA_ID') or os.getenv('BACK_CAMERA_ID') or os.getenv('VIDEO_SOURCE')
    if explicit:
        return explicit, os.getenv('FACE_CAMERA_WIDTH', '640'), os.getenv('FACE_CAMERA_HEIGHT', '640')
    config_path = Path(os.getenv('ROBOT_PROJECT_CONFIG', '/home/test/new_project/config/hardware.json'))
    try:
        import json as _json
        config = _json.loads(config_path.read_text(encoding='utf-8'))
        back = (config.get('cameras') or {}).get('back') or {}
        return str(back.get('device') or '/dev/video22'), str(back.get('width') or 640), str(back.get('height') or 480)
    except Exception:
        return '/dev/video22', '640', '640'

DEFAULT_CAMERA, DEFAULT_CAMERA_WIDTH, DEFAULT_CAMERA_HEIGHT = _project_back_camera_default()
PROJECT_CAR_WS = ASSETS_DIR / 'Car_real_copy'
CAR_WS = Path(os.getenv('CAR_REAL_WS', str(PROJECT_CAR_WS)))
CONTROLLER_CLI = Path(os.getenv('PET_CONTROLLER_CLI_PATH', str(CAR_WS / 'src/demo/controller_cli.py')))
os.environ.setdefault('FACE_CAMERA_ID', DEFAULT_CAMERA)
os.environ.setdefault('BACK_CAMERA_ID', DEFAULT_CAMERA)
os.environ.setdefault('FACE_CAMERA_WIDTH', DEFAULT_CAMERA_WIDTH)
os.environ.setdefault('FACE_CAMERA_HEIGHT', DEFAULT_CAMERA_HEIGHT)
os.environ.setdefault('PET_CONTROLLER_CLI_PATH', str(CONTROLLER_CLI))
os.environ.setdefault('PET_PRELOAD_FITNESS_MODELS', '0')


# ===== Silent speaker shim =====
_silent_speaker = types.ModuleType('speaker')
_silent_speaker._mp_q = None

def _silent_init_mp_queue(q=None):
    _silent_speaker._mp_q = q
    return True

def _silent_speak(text):
    return True

_silent_speaker.init_mp_queue = _silent_init_mp_queue
_silent_speaker.speak = _silent_speak
_silent_speaker.stop = lambda: True
_silent_speaker.release = lambda: True
sys.modules['speaker'] = _silent_speaker
speaker = _silent_speaker

def _ensure_package(package_name):
    if not package_name:
        return None
    pkg = sys.modules.get(package_name)
    if pkg is None:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = []
        sys.modules[package_name] = pkg
    parent_name, _, attr = package_name.rpartition('.')
    if parent_name:
        parent = _ensure_package(parent_name)
        setattr(parent, attr, pkg)
    return pkg


def _publish_namespace(module_name, before_names, *, top_level_aliases=()):
    module = types.ModuleType(module_name)
    module.__file__ = globals().get('__file__')
    module.__package__ = module_name.rpartition('.')[0]
    for key, value in list(globals().items()):
        if key.startswith('__') or key in before_names:
            continue
        if key in {'before_names', 'module_name', 'top_level_aliases'}:
            continue
        setattr(module, key, value)
    package_name, _, attr = module_name.rpartition('.')
    if package_name:
        package = _ensure_package(package_name)
        setattr(package, attr, module)
    sys.modules[module_name] = module
    globals()[attr] = module
    for alias in top_level_aliases:
        sys.modules[alias] = module
        globals()[alias.rpartition('.')[2]] = module
    return module


# ===== Inlined module: skills.runtime_config =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'single_function_runtime_config.py')

import os
import multiprocessing
from pathlib import Path
PROJECT_ROOT = Path(os.getenv('SINGLE_FUNCTION_ROOT', Path(__file__).resolve().parents[1]))
ASSETS_DIR = PROJECT_ROOT / 'assets'
MODEL_DIR = ASSETS_DIR / 'model'
MOVEMENT_DIR = ASSETS_DIR / 'movement_count_2'
FITNESS_SAMPLES_DIR = Path(os.getenv('FITNESS_SAMPLES_DIR', str(ASSETS_DIR / 'fitness_poses_csvs_out')))
MEDIAPIPE_MODELS_DIR = ASSETS_DIR / 'mediapipe_models'
RUNTIME_DIR = Path(os.getenv('SINGLE_FUNCTION_RUNTIME_DIR', str(PROJECT_ROOT / 'runtime')))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
FACE_CAMERA_ID = os.getenv('FACE_CAMERA_ID', '/dev/video22')
FACE_DB_PATH = os.getenv('FACE_DB_PATH', str(RUNTIME_DIR / 'faces.db'))
FACE_CAMERA_SHOW_WINDOW = os.getenv('FACE_CAMERA_SHOW_WINDOW', '0')
FACE_CAMERA_USE_SUBPROCESS = os.getenv('FACE_CAMERA_USE_SUBPROCESS', '0')
PET_TRACKING_RESULT_PATH = os.getenv('PET_TRACKING_RESULT_PATH', str(RUNTIME_DIR / 'pet_tracking_result.txt'))
PET_TRACKING_OUTPUT_VIDEO = os.getenv('PET_TRACKING_OUTPUT_VIDEO', str(RUNTIME_DIR / 'pet_tracking_record.mp4'))
PUSHUP_VIDEO_OUTPUT = os.getenv('PUSHUP_VIDEO_OUTPUT', str(RUNTIME_DIR / 'pushup_record.mp4'))
MP_START_METHOD = os.getenv('PET_MP_START_METHOD', 'fork' if os.name == 'posix' else 'spawn')
def get_mp_context():
    try:
        return multiprocessing.get_context(MP_START_METHOD)
    except ValueError:
        return multiprocessing.get_context()
CONTROLLER_CLI_PATH = os.getenv('PET_CONTROLLER_CLI_PATH', str(PROJECT_ROOT / 'assets' / 'Car_real_copy' / 'src' / 'demo' / 'controller_cli.py'))
STRICT_MODEL_PRELOAD = False
PRELOAD_FITNESS_MODELS = False
os.environ.setdefault('FACE_CAMERA_ID', FACE_CAMERA_ID)
os.environ.setdefault('FACE_CAMERA_SHOW_WINDOW', FACE_CAMERA_SHOW_WINDOW)
os.environ.setdefault('FACE_CAMERA_USE_SUBPROCESS', FACE_CAMERA_USE_SUBPROCESS)
os.environ.setdefault('PET_TRACKING_RESULT_PATH', PET_TRACKING_RESULT_PATH)
os.environ.setdefault('PET_TRACKING_OUTPUT_VIDEO', PET_TRACKING_OUTPUT_VIDEO)
os.environ.setdefault('PET_CONTROLLER_CLI_PATH', CONTROLLER_CLI_PATH)
__file__ = _run_file
_publish_namespace('skills.runtime_config', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: skills.runtime_models =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'single_function_runtime_models.py')
import atexit
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass
class ModelEntry:
    name: str
    instance: object
    persistent: bool = True
    description: str = ""


_lock = threading.RLock()
_models: Dict[str, ModelEntry] = {}


def register_model(name: str, instance, *, persistent: bool = True, description: str = ""):
    if instance is None:
        raise ValueError(f"cannot register empty model: {name}")
    with _lock:
        _models[name] = ModelEntry(
            name=name,
            instance=instance,
            persistent=persistent,
            description=description,
        )
    return instance


def get_model(name: str, default=None):
    with _lock:
        entry = _models.get(name)
        return entry.instance if entry is not None else default


def has_model(name: str) -> bool:
    with _lock:
        return name in _models and _models[name].instance is not None


def get_or_create(
    name: str,
    factory: Callable[[], object],
    *,
    persistent: bool = True,
    description: str = "",
):
    with _lock:
        entry = _models.get(name)
        if entry is not None and entry.instance is not None:
            return entry.instance

    instance = factory()
    return register_model(name, instance, persistent=persistent, description=description)


def acquire_or_create(
    name: str,
    factory: Callable[[], object],
    *,
    description: str = "",
):
    model = get_model(name)
    if model is not None:
        return model, False
    return factory(), True


def summary():
    with _lock:
        return {
            name: {
                "type": type(entry.instance).__name__,
                "persistent": entry.persistent,
                "description": entry.description,
            }
            for name, entry in _models.items()
        }


def release_all():
    with _lock:
        entries = list(_models.values())
        _models.clear()

    for entry in entries:
        if not entry.persistent:
            continue
        release = getattr(entry.instance, "release", None)
        close = getattr(entry.instance, "close", None)
        try:
            if callable(release):
                release()
            elif callable(close):
                close()
        except Exception as exc:
            print(f"[RuntimeModels] 释放模型失败: {entry.name}: {exc}")


atexit.register(release_all)
__file__ = _run_file
_publish_namespace('skills.runtime_models', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: movement_count_2.poseembedding =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'movement_count_2/poseembedding.py')
import numpy as np


# 人体姿态编码模块
class FullBodyPoseEmbedder(object):
    """Converts 3D pose landmarks into 3D embedding."""

    def __init__(self, torso_size_multiplier=2.5):
        # Multiplier to apply to the torso to get minimal body size.
        # 乘数应用于躯干以获得最小的身体尺寸
        self._torso_size_multiplier = torso_size_multiplier

        # Names of the landmarks as they appear in the prediction.
        # 出现在预测中的landmarks名称。
        self._landmark_names = [
            'nose',
            'left_eye_inner', 'left_eye', 'left_eye_outer',
            'right_eye_inner', 'right_eye', 'right_eye_outer',
            'left_ear', 'right_ear',
            'mouth_left', 'mouth_right',
            'left_shoulder', 'right_shoulder',
            'left_elbow', 'right_elbow',
            'left_wrist', 'right_wrist',
            'left_pinky_1', 'right_pinky_1',
            'left_index_1', 'right_index_1',
            'left_thumb_2', 'right_thumb_2',
            'left_hip', 'right_hip',
            'left_knee', 'right_knee',
            'left_ankle', 'right_ankle',
            'left_heel', 'right_heel',
            'left_foot_index', 'right_foot_index',
        ]

    def __call__(self, landmarks):
        """Normalizes pose landmarks and converts to embedding
        归一化姿势landmarks并转换为embedding

        Args:
          landmarks - NumPy array with 3D landmarks of shape (N, 3).

        Result:
          Numpy array with pose embedding of shape (M, 3) where `M` is the number of
          pairwise distances defined in `_get_pose_distance_embedding`.
          具有形状 (M, 3) 的姿势embedding的 Numpy 数组，其中“M”是“_get_pose_distance_embedding”中定义的成对距离的数量。
        """
        assert landmarks.shape[0] == len(self._landmark_names), 'Unexpected number of landmarks: {}'.format(
            landmarks.shape[0])

        # 获取 landmarks.
        landmarks = np.copy(landmarks)

        # Normalize landmarks.
        landmarks = self._normalize_pose_landmarks(landmarks)

        # Get embedding.
        embedding = self._get_pose_distance_embedding(landmarks)

        return embedding

    def _normalize_pose_landmarks(self, landmarks):
        """Normalizes landmarks translation and scale.归一化landmarks的平移和缩放"""
        landmarks = np.copy(landmarks)

        # Normalize translation.
        pose_center = self._get_pose_center(landmarks)
        landmarks -= pose_center

        # Normalize scale.
        pose_size = self._get_pose_size(landmarks, self._torso_size_multiplier)
        landmarks /= pose_size
        # Multiplication by 100 is not required, but makes it eaasier to debug.
        landmarks *= 100

        return landmarks

    def _get_pose_center(self, landmarks):
        """Calculates pose center as point between hips.将姿势中心计算为臀部之间的点。"""
        left_hip = landmarks[self._landmark_names.index('left_hip')]
        right_hip = landmarks[self._landmark_names.index('right_hip')]
        center = (left_hip + right_hip) * 0.5
        return center

    def _get_pose_size(self, landmarks, torso_size_multiplier):
        """Calculates pose size.计算姿势大小。

        它是下面两个值的最大值:
          * 躯干大小乘以`torso_size_multiplier`
          * 从姿势中心到任何姿势地标的最大距离
        """
        # 这种方法仅使用 2D landmarks来计算姿势大小.
        landmarks = landmarks[:, :2]

        # 臀部中心。
        left_hip = landmarks[self._landmark_names.index('left_hip')]
        right_hip = landmarks[self._landmark_names.index('right_hip')]
        hips = (left_hip + right_hip) * 0.5

        # 两肩中心。
        left_shoulder = landmarks[self._landmark_names.index('left_shoulder')]
        right_shoulder = landmarks[self._landmark_names.index('right_shoulder')]
        shoulders = (left_shoulder + right_shoulder) * 0.5

        # 躯干尺寸作为最小的身体尺寸。
        torso_size = np.linalg.norm(shoulders - hips)

        # 到姿势中心的最大距离。
        pose_center = self._get_pose_center(landmarks)
        max_dist = np.max(np.linalg.norm(landmarks - pose_center, axis=1))

        return max(torso_size * torso_size_multiplier, max_dist)

    def _get_pose_distance_embedding(self, landmarks):
        """Converts pose landmarks into 3D embedding.
            将姿势landmarks转换为 3D embedding.
        我们使用几个成对的 3D 距离来形成姿势embedding。 所有距离都包括带符号的 X 和 Y 分量。
        我们使用不同类型的对来覆盖不同的姿势类别。 Feel free to remove some or add new.

        Args:
          landmarks - NumPy array with 3D landmarks of shape (N, 3).

        Result:
          Numpy array with pose embedding of shape (M, 3) where `M` is the number of
          pairwise distances.
        """
        embedding = np.array([
            # One joint.

            self._get_distance(
                self._get_average_by_names(landmarks, 'left_hip', 'right_hip'),
                self._get_average_by_names(landmarks, 'left_shoulder', 'right_shoulder')),

            self._get_distance_by_names(landmarks, 'left_shoulder', 'left_elbow'),
            self._get_distance_by_names(landmarks, 'right_shoulder', 'right_elbow'),

            self._get_distance_by_names(landmarks, 'left_elbow', 'left_wrist'),
            self._get_distance_by_names(landmarks, 'right_elbow', 'right_wrist'),

            self._get_distance_by_names(landmarks, 'left_hip', 'left_knee'),
            self._get_distance_by_names(landmarks, 'right_hip', 'right_knee'),

            self._get_distance_by_names(landmarks, 'left_knee', 'left_ankle'),
            self._get_distance_by_names(landmarks, 'right_knee', 'right_ankle'),

            # Two joints.

            self._get_distance_by_names(landmarks, 'left_shoulder', 'left_wrist'),
            self._get_distance_by_names(landmarks, 'right_shoulder', 'right_wrist'),

            self._get_distance_by_names(landmarks, 'left_hip', 'left_ankle'),
            self._get_distance_by_names(landmarks, 'right_hip', 'right_ankle'),

            # Four joints.

            self._get_distance_by_names(landmarks, 'left_hip', 'left_wrist'),
            self._get_distance_by_names(landmarks, 'right_hip', 'right_wrist'),

            # Five joints.

            self._get_distance_by_names(landmarks, 'left_shoulder', 'left_ankle'),
            self._get_distance_by_names(landmarks, 'right_shoulder', 'right_ankle'),

            self._get_distance_by_names(landmarks, 'left_hip', 'left_wrist'),
            self._get_distance_by_names(landmarks, 'right_hip', 'right_wrist'),

            # Cross body.

            self._get_distance_by_names(landmarks, 'left_elbow', 'right_elbow'),
            self._get_distance_by_names(landmarks, 'left_knee', 'right_knee'),

            self._get_distance_by_names(landmarks, 'left_wrist', 'right_wrist'),
            self._get_distance_by_names(landmarks, 'left_ankle', 'right_ankle'),

            # Body bent direction.

            # self._get_distance(
            #     self._get_average_by_names(landmarks, 'left_wrist', 'left_ankle'),
            #     landmarks[self._landmark_names.index('left_hip')]),
            # self._get_distance(
            #     self._get_average_by_names(landmarks, 'right_wrist', 'right_ankle'),
            #     landmarks[self._landmark_names.index('right_hip')]),
        ])

        return embedding

    def _get_average_by_names(self, landmarks, name_from, name_to):
        lmk_from = landmarks[self._landmark_names.index(name_from)]
        lmk_to = landmarks[self._landmark_names.index(name_to)]
        return (lmk_from + lmk_to) * 0.5

    def _get_distance_by_names(self, landmarks, name_from, name_to):
        lmk_from = landmarks[self._landmark_names.index(name_from)]
        lmk_to = landmarks[self._landmark_names.index(name_to)]
        return self._get_distance(lmk_from, lmk_to)

    def _get_distance(self, lmk_from, lmk_to):
        return lmk_to - lmk_from
__file__ = _run_file
_publish_namespace('movement_count_2.poseembedding', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: movement_count_2.poseclassifier =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'movement_count_2/poseclassifier.py')
import numpy as np
import os
import csv


# 人体姿态分类
class PoseSample(object):

    def __init__(self, name, landmarks, class_name, embedding):
        self.name = name
        self.landmarks = landmarks
        self.class_name = class_name

        self.embedding = embedding


class PoseSampleOutlier(object):

    def __init__(self, sample, detected_class, all_classes):
        self.sample = sample
        self.detected_class = detected_class
        self.all_classes = all_classes



class PoseClassifier(object):
    """对landmarks进行分类."""

    def __init__(self,
                 pose_samples_folder,
                 pose_embedder,
                 class_name,
                 file_extension='csv',
                 file_separator=',',
                 n_landmarks=33,
                 n_dimensions=3,
                 top_n_by_max_distance=30,
                 top_n_by_mean_distance=10,
                 axes_weights=(1., 1., 0.2)):
        self._pose_embedder = pose_embedder
        self._n_landmarks = n_landmarks
        self._n_dimensions = n_dimensions
        # KNN算法中的K
        self._top_n_by_max_distance = top_n_by_max_distance
        self._top_n_by_mean_distance = top_n_by_mean_distance
        self._axes_weights = axes_weights

        self._pose_samples = self._load_pose_samples(pose_samples_folder,
                                                     class_name,
                                                     file_extension,
                                                     file_separator,
                                                     n_landmarks,
                                                     n_dimensions,
                                                     pose_embedder)

    def _load_pose_samples(self,
                           pose_samples_folder,
                           class_n,
                           file_extension,
                           file_separator,
                           n_landmarks,
                           n_dimensions,
                           pose_embedder):
        """Loads pose samples from a given folder.

        Required folder structure:
          neutral_standing.csv
          pushups_down.csv
          pushups_up.csv
          squats_down.csv
          ...

        Required CSV structure:
          sample_00001,x1,y1,z1,x2,y2,z2,....
          sample_00002,x1,y1,z1,x2,y2,z2,....
          ...
        """
        classname = class_n.split('_')[0]
        # 文件夹中的每个文件代表一个姿势类.
        file_names = [name for name in os.listdir(pose_samples_folder) if classname in name and name.endswith(file_extension)]

        pose_samples = []
        for file_name in file_names:
            # 使用文件名作为姿势类名称.
            class_name = file_name[:-(len(file_extension) + 1)]

            # Parse CSV.
            with open(os.path.join(pose_samples_folder, file_name)) as csv_file:
                csv_reader = csv.reader(csv_file, delimiter=file_separator)
                for row in csv_reader:
                    assert len(row) == n_landmarks * n_dimensions + 1, 'Wrong number of values: {}'.format(len(row))
                    landmarks = np.array(row[1:], np.float32).reshape([n_landmarks, n_dimensions])
                    pose_samples.append(PoseSample(
                        name=row[0],
                        landmarks=landmarks,
                        class_name=class_name,
                        embedding=pose_embedder(landmarks),
                    ))

        return pose_samples

    def find_pose_sample_outliers(self):
        """针对整个数据库对每个样本进行分类."""
        # 找出目标姿势中的异常值
        outliers = []
        for sample in self._pose_samples:
            # 为目标找到最近的姿势。
            pose_landmarks = sample.landmarks.copy()
            pose_classification = self.__call__(pose_landmarks)
            class_names = [class_name for class_name, count in pose_classification.items() if
                           count == max(pose_classification.values())]

            # 如果最近的姿势具有不同的类别或多个姿势类别被检测为最近，则样本是异常值。
            if sample.class_name not in class_names or len(class_names) != 1:
                outliers.append(PoseSampleOutlier(sample, class_names, pose_classification))

        return outliers

    def __call__(self, pose_landmarks):
        """对给定的姿势进行分类。

        分类分两个阶段完成:
          * 首先，我们按 MAX 距离选取前 N 个样本。 它允许删除与给定姿势几乎相同但有一些关节在向一个方向弯曲的样本。
          * 然后我们按平均距离选择前 N 个样本。 在上一步移除异常值后， 我们可以选择在平均值上接近的样本。

        Args（参数）:
          pose_landmarks: NumPy array with 3D landmarks of shape (N, 3).具有形状 (N, 3) 的 3D landmarks的 NumPy 数组

        Returns:
          Dictionary with count of nearest pose samples from the database.含数据库中最近姿势样本计数的字典 Sample:
            {
              'pushups_down': 8,
              'pushups_up': 2,
            }
        """
        # 检查提供的姿势和目标姿势是否具有相同的形状.
        assert pose_landmarks.shape == (self._n_landmarks, self._n_dimensions), 'Unexpected shape: {}'.format(
            pose_landmarks.shape)

        # 获取给定姿势的 embedding.
        pose_embedding = self._pose_embedder(pose_landmarks)
        flipped_pose_embedding = self._pose_embedder(pose_landmarks * np.array([-1, 1, 1]))

        # 按最大距离过滤。
        # 这有助于去除异常值——与给定的姿势几乎相同，但一个关节弯曲到另一个方向，实际上代表不同的姿势类别。
        max_dist_heap = []
        for sample_idx, sample in enumerate(self._pose_samples):
            max_dist = min(
                np.max(np.abs(sample.embedding - pose_embedding) * self._axes_weights),
                np.max(np.abs(sample.embedding - flipped_pose_embedding) * self._axes_weights),
            )
            max_dist_heap.append([max_dist, sample_idx])

        max_dist_heap = sorted(max_dist_heap, key=lambda x: x[0])
        max_dist_heap = max_dist_heap[:self._top_n_by_max_distance]

        # 按平均距离过滤。
        # 去除异常值后，我们可以通过平均距离找到最近的姿势。
        mean_dist_heap = []
        for _, sample_idx in max_dist_heap:
            sample = self._pose_samples[sample_idx]
            mean_dist = min(
                np.mean(np.abs(sample.embedding - pose_embedding) * self._axes_weights),
                np.mean(np.abs(sample.embedding - flipped_pose_embedding) * self._axes_weights),
            )
            mean_dist_heap.append([mean_dist, sample_idx])

        mean_dist_heap = sorted(mean_dist_heap, key=lambda x: x[0])
        mean_dist_heap = mean_dist_heap[:self._top_n_by_mean_distance]

        # Collect results into map: (class_name -> n_samples)
        class_names = [self._pose_samples[sample_idx].class_name for _, sample_idx in mean_dist_heap]
        result = {class_name: class_names.count(class_name) for class_name in set(class_names)}

        return result
__file__ = _run_file
_publish_namespace('movement_count_2.poseclassifier', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: movement_count_2.resultsmooth =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'movement_count_2/resultsmooth.py')
# 姿态分类结果平滑
class EMADictSmoothing(object):
    """平滑姿势分类。"""

    def __init__(self, window_size=10, alpha=0.2):
        self._window_size = window_size
        self._alpha = alpha

        self._data_in_window = []

    def __call__(self, data):
        """平滑给定的姿势分类。

        平滑是通过计算在给定时间窗口中观察到的每个姿势类别的指数移动平均值来完成的。错过的姿势类将替换为 0。

        Args:
          data: Dictionary with pose classification. Sample:
              {
                'pushups_down': 8,
                'pushups_up': 2,
              }

        Result:
          Dictionary in the same format but with smoothed and float instead of
          integer values. Sample:
            {
              'pushups_down': 8.3,
              'pushups_up': 1.7,
            }
        """
        # 将新数据添加到窗口的开头以获得更简单的代码.
        self._data_in_window.insert(0, data)
        self._data_in_window = self._data_in_window[:self._window_size]

        # Get all keys.
        keys = set([key for data in self._data_in_window for key, _ in data.items()])

        # Get smoothed values.
        smoothed_data = dict()
        for key in keys:
            factor = 1.0
            top_sum = 0.0
            bottom_sum = 0.0
            for data in self._data_in_window:
                value = data[key] if key in data else 0.0

                top_sum += factor * value
                bottom_sum += factor

                # Update factor.
                factor *= (1.0 - self._alpha)

            smoothed_data[key] = top_sum / bottom_sum

        return smoothed_data
__file__ = _run_file
_publish_namespace('movement_count_2.resultsmooth', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: movement_count_2.counter =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'movement_count_2/counter.py')
# 动作计数器
class RepetitionCounter(object):
    # 计算给定目标姿势类的重复次数

    def __init__(self, class_name, enter_threshold=6, exit_threshold=4):
        self._class_name = class_name

        # 如果姿势通过了给定的阈值，那么我们就进入该动作的计数
        self._enter_threshold = enter_threshold
        self._exit_threshold = exit_threshold

        # 是否处于给定的姿势
        self._pose_entered = False

        # 退出姿势的次数
        self._n_repeats = 0

    @property
    def n_repeats(self):
        return self._n_repeats

    def __call__(self, pose_classification):
        # 计算给定帧之前发生的重复次数
        # 我们使用两个阈值。首先，您需要从较高的位置上方进入姿势，然后您需要从较低的位置下方退出。
        # 阈值之间的差异使其对预测抖动稳定（如果只有一个阈值，则会导致错误计数）。

        # 参数：
        #   pose_classification：当前帧上的姿势分类字典
        #         Sample:
        #         {
        #             'squat_down': 8.3,
        #             'squat_up': 1.7,
        #         }

        # 获取姿势的置信度.
        pose_confidence = 0.0
        if self._class_name in pose_classification:
            pose_confidence = pose_classification[self._class_name]

        # On the very first frame or if we were out of the pose, just check if we
        # entered it on this frame and update the state.
        # 在第一帧或者如果我们不处于姿势中，只需检查我们是否在这一帧上进入该姿势并更新状态
        if not self._pose_entered:
            self._pose_entered = pose_confidence > self._enter_threshold
            return self._n_repeats

        # 如果我们处于姿势并且正在退出它，则增加计数器并更新状态
        if pose_confidence < self._exit_threshold:
            self._n_repeats += 1
            self._pose_entered = False

        return self._n_repeats
__file__ = _run_file
_publish_namespace('movement_count_2.counter', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: movement_count_2.visualizer =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'movement_count_2/visualizer.py')
from PIL import Image
# import requests
import io
from matplotlib import pyplot as plt
from PIL import ImageDraw
from PIL import ImageFont


# 分类结果可视化
class PoseClassificationVisualizer(object):
    """Keeps track of claassifcations for every frame and renders them."""

    def __init__(self,
                 class_name,
                 plot_location_x=0.05,
                 plot_location_y=0.05,
                 plot_max_width=0.4,
                 plot_max_height=0.4,
                 plot_figsize=(9, 4),
                 plot_x_max=None,
                 plot_y_max=None,
                 counter_location_x=0.80,
                 counter_location_y=0.05,
                 #                counter_font_path='https://github.com/googlefonts/roboto/blob/main/src/hinted/Roboto-Regular.ttf?raw=true',
                 counter_font_color='red',
                 counter_font_size=0.1):
        self._class_name = class_name
        self._plot_location_x = plot_location_x
        self._plot_location_y = plot_location_y
        self._plot_max_width = plot_max_width
        self._plot_max_height = plot_max_height
        self._plot_figsize = plot_figsize
        self._plot_x_max = plot_x_max
        self._plot_y_max = plot_y_max
        self._counter_location_x = counter_location_x
        self._counter_location_y = counter_location_y
        #     self._counter_font_path = counter_font_path
        self._counter_font_color = counter_font_color
        self._counter_font_size = counter_font_size

        self._counter_font = None

        self._pose_classification_history = []
        self._pose_classification_filtered_history = []

    def __call__(self,
                 frame,
                 pose_classification,
                 pose_classification_filtered,
                 repetitions_count):
        """Renders pose classifcation and counter until given frame."""
        # Extend classification history.
        self._pose_classification_history.append(pose_classification)
        self._pose_classification_filtered_history.append(pose_classification_filtered)

        # Output frame with classification plot and counter.
        output_img = Image.fromarray(frame)

        output_width = output_img.size[0]
        output_height = output_img.size[1]

        # Draw the plot.
        img = self._plot_classification_history(output_width, output_height)
        img.thumbnail((int(output_width * self._plot_max_width),
                       int(output_height * self._plot_max_height)),
                      Image.ANTIALIAS)
        output_img.paste(img,
                         (int(output_width * self._plot_location_x),
                          int(output_height * self._plot_location_y)))

        # Draw the count.
        output_img_draw = ImageDraw.Draw(output_img)
        if self._counter_font is None:
            font_size = int(output_height * self._counter_font_size)
            #       font_request = requests.get(self._counter_font_path, allow_redirects=True)
            self._counter_font = ImageFont.truetype('Roboto-Regular.ttf', size=font_size)
        output_img_draw.text((output_width * self._counter_location_x,
                              output_height * self._counter_location_y),
                             str(repetitions_count),
                             font=self._counter_font,
                             fill=self._counter_font_color)

        return output_img

    def _plot_classification_history(self, output_width, output_height):
        fig = plt.figure(figsize=self._plot_figsize)

        for classification_history in [self._pose_classification_history,
                                       self._pose_classification_filtered_history]:
            y = []
            for classification in classification_history:
                if classification is None:
                    y.append(None)
                elif self._class_name in classification:
                    y.append(classification[self._class_name])
                else:
                    y.append(0)
            plt.plot(y, linewidth=7)

        plt.grid(axis='y', alpha=0.75)
        plt.xlabel('Frame')
        plt.ylabel('Confidence')
        plt.title('Classification history for `{}`'.format(self._class_name))
        # plt.legend(loc='upper right')

        if self._plot_y_max is not None:
            plt.ylim(top=self._plot_y_max)
        if self._plot_x_max is not None:
            plt.xlim(right=self._plot_x_max)

        # Convert plot to image.
        buf = io.BytesIO()
        dpi = min(
            output_width * self._plot_max_width / float(self._plot_figsize[0]),
            output_height * self._plot_max_height / float(self._plot_figsize[1]))
        fig.savefig(buf, dpi=dpi)
        buf.seek(0)
        img = Image.open(buf)
        plt.close()

        return img
__file__ = _run_file
_publish_namespace('movement_count_2.visualizer', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: movement_count_2.squat_camera_base_fps_1 =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'movement_count_2/squat_camera_base_fps_1.py')
import os
import sys

# ============================================================
# 路径修复：
# 当前文件一般位于：
# /home/test/refine_0508/llm/movement_count_2/xxx.py
#
# 为了导入：
# from skills import runtime_config, runtime_models
# 需要加入：
# /home/test/refine_0508
#
# 为了导入：
# poseembedding / poseclassifier / resultsmooth / counter
# 需要加入：
# 当前目录下的 code 目录
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
CODE_DIR = SCRIPT_DIR
POSE_SAMPLES_DIR = os.environ.get("FITNESS_SAMPLES_DIR", os.path.join(PROJECT_ROOT, "fitness_poses_csvs_out"))

for _path in (PROJECT_ROOT, CODE_DIR):
    _path = os.path.abspath(_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cv2
import numpy as np
import time
import signal
import threading
import multiprocessing
import warnings
from typing import Optional

from skills import runtime_config, runtime_models

def _install_protobuf_message_factory_compat():
    """
    MediaPipe packet_getter in this image expects protobuf's module-level
    message_factory.GetMessageClass, but protobuf 3.20.x only exposes
    MessageFactory.GetPrototype. Add the missing API before MediaPipe is used.
    """
    try:
        from google.protobuf import message_factory
    except Exception:
        return

    if hasattr(message_factory, "GetMessageClass"):
        return

    factory = message_factory.MessageFactory()

    def _get_message_class(descriptor):
        return factory.GetPrototype(descriptor)

    message_factory.GetMessageClass = _get_message_class


_install_protobuf_message_factory_compat()

from mediapipe.python.solutions import pose as mp_pose

from movement_count_2 import poseembedding as pe
from movement_count_2 import poseclassifier as pc
from movement_count_2 import resultsmooth as rs
from movement_count_2 import counter

warnings.filterwarnings("ignore")


def _safe_remove_file(file_path: str) -> None:
    if not file_path or not os.path.exists(file_path):
        return

    try:
        os.remove(file_path)
    except OSError:
        try:
            with open(file_path, "w") as f:
                f.write("")
        except OSError:
            pass


def _camera_flip_code():
    value = os.environ.get(
        "SQUAT_CAMERA_FLIP",
        os.environ.get("FACE_CAMERA_FLIP", ""),
    ).strip().lower()
    if value in {"", "none", "off", "false", "no"}:
        return None
    if value in {"vertical", "v", "updown", "ud", "0"}:
        return 0
    if value in {"horizontal", "h", "leftright", "lr", "1"}:
        return 1
    if value in {"both", "rotate180", "180", "hv", "vh", "-1"}:
        return -1
    return None


def _is_valid_bgr_frame(frame) -> bool:
    if frame is None or not isinstance(frame, np.ndarray):
        return False
    if frame.ndim not in (2, 3):
        return False
    if frame.shape[0] <= 0 or frame.shape[1] <= 0:
        return False
    if frame.ndim == 3 and frame.shape[2] not in (1, 3, 4):
        return False
    return True


def _section_to_chinese(section: int) -> str:
    digits = "零一二三四五六七八九"
    units = ["", "十", "百", "千"]

    result = ""
    zero_pending = False

    for idx in range(3, -1, -1):
        base = 10 ** idx
        digit = section // base
        section %= base

        if digit == 0:
            if result:
                zero_pending = True
            continue

        if zero_pending:
            result += "零"
            zero_pending = False

        if not (digit == 1 and idx == 1 and not result):
            result += digits[digit]

        result += units[idx]

    return result or "零"


def number_to_chinese(num: int) -> str:
    if num == 0:
        return "零"

    if num < 0:
        return f"负{number_to_chinese(-num)}"

    section_units = ["", "万", "亿", "兆"]
    sections = []

    while num > 0:
        sections.append(num % 10000)
        num //= 10000

    result = ""
    need_zero = False

    for idx in range(len(sections) - 1, -1, -1):
        section = sections[idx]

        if section == 0:
            need_zero = bool(result)
            continue

        if need_zero or (result and section < 1000):
            result += "零"

        result += _section_to_chinese(section) + section_units[idx]
        need_zero = False

    return result


def squat_preproc(cv_img):
    """
    预处理图像为 RGB，用于 MediaPipe。
    """
    if not _is_valid_bgr_frame(cv_img):
        raise ValueError(f"非法图像输入，shape={getattr(cv_img, 'shape', None)}")

    if cv_img.ndim == 2:
        cv_img = cv2.cvtColor(cv_img, cv2.COLOR_GRAY2BGR)
    elif cv_img.ndim == 3 and cv_img.shape[2] == 1:
        cv_img = cv2.cvtColor(cv_img, cv2.COLOR_GRAY2BGR)
    elif cv_img.ndim == 3 and cv_img.shape[2] == 4:
        cv_img = cv2.cvtColor(cv_img, cv2.COLOR_BGRA2BGR)

    return cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)


class SquatCountingSystem:
    """
    深蹲/下蹲计数系统。

    对外主调用方法：
        1. start_squat_counting(video_source)
           开始深蹲计数。

        2. query_squat_count()
           查询当前深蹲个数。

        3. stop_squat_counting()
           结束深蹲计数并汇总。

    兼容旧方法：
        start_counting(video_source)
        query_progress()
        stop_and_summarize()

    Board 生命周期原则：
        1. 不在 __init__ 中初始化 Board。
        2. 当前深蹲计数默认不需要轮子、步进电机和天问模块，所以默认不创建 Board。
        3. 如果未来某个计数功能需要用到底轮、步进电机或天问模块，
           只在该功能开始时初始化 Board，功能结束后在 finally 中释放 Board。
    """

    DEFAULT_PID_FILE = "/tmp/squat_pid.txt"
    DEFAULT_COUNT_FILE = "/tmp/squat_count.txt"

    SESSION_SECONDS = 30
    IDLE_SECONDS = 10

    class DummyBoard:
        """
        Board 初始化失败时使用的虚拟 Board。
        """

        def set_motor_speed(self, speeds):
            pass

        def set_single_motor_speed(self, motor_id, speed):
            pass

        def get_wkup(self):
            return None

    class SingleSubjectGate:
        def __init__(self, max_center_shift=0.30, update_alpha=0.08):
            env_enabled = os.getenv("FITNESS_LOCK_SINGLE_PERSON", "1")
            self.enabled = env_enabled.strip().lower() not in {"0", "false", "no", "off"}
            self.max_center_shift = float(os.getenv("FITNESS_LOCK_MAX_CENTER_SHIFT", max_center_shift))
            self.update_alpha = float(os.getenv("FITNESS_LOCK_UPDATE_ALPHA", update_alpha))
            self.locked_bbox = None
            self.rejected_frames = 0

        @staticmethod
        def _bbox_from_pose_result(pose_result):
            pose_landmarks = getattr(pose_result, "pose_landmarks", None)
            if pose_landmarks is None:
                return None

            points = []
            for lm in pose_landmarks.landmark:
                if getattr(lm, "visibility", 1.0) >= 0.35:
                    points.append((float(lm.x), float(lm.y)))

            if len(points) < 8:
                return None

            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            return (
                (min_x + max_x) * 0.5,
                (min_y + max_y) * 0.5,
                max(max_x - min_x, 1e-6),
                max(max_y - min_y, 1e-6),
            )

        def accept(self, pose_result):
            if not self.enabled:
                return True

            bbox = self._bbox_from_pose_result(pose_result)
            if bbox is None:
                return False

            if self.locked_bbox is None:
                self.locked_bbox = bbox
                print("[SingleSubjectGate] locked counting target")
                return True

            prev_cx, prev_cy, prev_w, prev_h = self.locked_bbox
            cx, cy, w, h = bbox
            center_shift = float(np.hypot(cx - prev_cx, cy - prev_cy))
            prev_area = max(prev_w * prev_h, 1e-6)
            area = max(w * h, 1e-6)
            area_ratio = max(prev_area / area, area / prev_area)

            if center_shift > self.max_center_shift or area_ratio > 3.0:
                self.rejected_frames += 1
                if self.rejected_frames == 1 or self.rejected_frames % 30 == 0:
                    print("[SingleSubjectGate] possible target switch; skip this frame")
                return False

            alpha = self.update_alpha
            self.locked_bbox = (
                prev_cx * (1.0 - alpha) + cx * alpha,
                prev_cy * (1.0 - alpha) + cy * alpha,
                prev_w * (1.0 - alpha) + w * alpha,
                prev_h * (1.0 - alpha) + h * alpha,
            )
            self.rejected_frames = 0
            return True

    class CameraReader:
        """
        后台摄像头读取器。
        """

        def __init__(self, src=0, width=640, height=640):
            self.cap = cv2.VideoCapture(src)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            self.flip_code = _camera_flip_code()

            if not self.cap.isOpened():
                raise RuntimeError(f"无法打开摄像头流: {src}")

            self._lock = threading.Lock()
            self._stop = threading.Event()

            self.ret, self.frame = self.cap.read()
            if self.ret and self.frame is not None:
                self.frame = self._apply_orientation(self.frame)

            self.thread = threading.Thread(
                target=self._update,
                daemon=True,
                name="squat-camera-reader",
            )
            self.thread.start()

        def _apply_orientation(self, frame):
            if self.flip_code is None:
                return frame
            return cv2.flip(frame, self.flip_code)

        def _update(self):
            while not self._stop.is_set():
                ret, frame = self.cap.read()

                if ret and frame is not None:
                    with self._lock:
                        self.ret = ret
                        self.frame = self._apply_orientation(frame)
                else:
                    time.sleep(0.005)

            if self.cap.isOpened():
                self.cap.release()

        def read(self):
            with self._lock:
                if self.frame is not None:
                    return self.ret, self.frame.copy()

            return False, None

        def isOpened(self):
            return not self._stop.is_set() and self.cap.isOpened()

        def release(self):
            self._stop.set()

            try:
                if self.thread.is_alive():
                    self.thread.join(timeout=2.0)
            except Exception:
                pass

            try:
                if self.cap.isOpened():
                    self.cap.release()
            except Exception:
                pass

    class SquatDet:
        """
        深蹲/下蹲姿态检测器。
        """

        def __init__(self, pose_samples_folder=POSE_SAMPLES_DIR):
            self.pose_tracker = mp_pose.Pose(
                static_image_mode=False,
                model_complexity=1,
                smooth_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )

            self.pose_embedder = pe.FullBodyPoseEmbedder()

            self.pose_classifier = pc.PoseClassifier(
                pose_samples_folder=pose_samples_folder,
                class_name="squat_down",
                pose_embedder=self.pose_embedder,
                top_n_by_max_distance=30,
                top_n_by_mean_distance=10,
            )

            self.pose_classification_filter = rs.EMADictSmoothing(
                window_size=10,
                alpha=0.2,
            )

            self.last_result = None

        def infer(self, cv_img):
            image = squat_preproc(cv_img)
            result = self.pose_tracker.process(image)
            self.last_result = result

            if result.pose_landmarks is None:
                return {}

            pose_landmarks = result.pose_landmarks.landmark
            landmarks = np.array([[lm.x, lm.y, lm.z] for lm in pose_landmarks])

            if landmarks.shape != (33, 3):
                return {}

            pose_classification = self.pose_classifier(landmarks)
            pose_classification_filtered = self.pose_classification_filter(pose_classification)

            return pose_classification_filtered

        def release(self):
            try:
                self.pose_tracker.close()
            except Exception:
                pass

    def __init__(
        self,
        pid_file: str = DEFAULT_PID_FILE,
        count_file: str = DEFAULT_COUNT_FILE,
    ):
        self.pid_file = pid_file
        self.count_file = count_file
        self._process: Optional[multiprocessing.Process] = None

    # ============================================================
    # Board 生命周期管理
    # ============================================================

    @staticmethod
    def _create_board():
        """
        只在某个功能真正需要底轮、步进电机、天问模块时才创建 Board。

        当前深蹲计数默认不需要 Board，所以默认不会调用该函数。
        后续如果需要例如：
            - 计数时自动调整机器人位置；
            - 使用步进电机调整摄像头角度；
            - 使用天问模块做唤醒检测；
        再在对应后台任务中设置 need_board=True。
        """
        try:
            from test3 import Board

            board = Board()
            print("[SquatCountingSystem] Board 已初始化，仅当前功能使用")
            return board

        except ImportError:
            print("[SquatCountingSystem] 底盘驱动 test3.Board 导入失败，使用虚拟 Board")
            return SquatCountingSystem.DummyBoard()

        except Exception as e:
            print(f"[SquatCountingSystem] Board 初始化失败，使用虚拟 Board: {e}")
            return SquatCountingSystem.DummyBoard()

    @staticmethod
    def _release_board(board):
        """
        手动释放当前功能创建的 Board。

        释放顺序：
            1. 停止底轮；
            2. 停止第三路电机/步进电机；
            3. 关闭 Board 接收线程标志；
            4. 调用 Board 暴露的 release/close/shutdown/stop；
            5. 关闭 Board.port 串口。
        """
        if board is None:
            return

        try:
            if hasattr(board, "set_motor_speed"):
                board.set_motor_speed([[1, 0], [2, 0]])
                time.sleep(0.03)
        except Exception:
            pass

        try:
            if hasattr(board, "set_single_motor_speed"):
                board.set_single_motor_speed(3, 0)
                time.sleep(0.03)
        except Exception:
            pass

        try:
            if hasattr(board, "enable_recv"):
                board.enable_recv = False
        except Exception:
            pass

        for method_name in ("release", "close", "shutdown", "stop"):
            try:
                method = getattr(board, method_name, None)
                if callable(method):
                    method()
                    print(f"[SquatCountingSystem] Board.{method_name}() 已调用")
                    return
            except Exception as e:
                print(f"[SquatCountingSystem] 调用 Board.{method_name}() 失败: {e}")

        try:
            port = getattr(board, "port", None)
            if port is not None and hasattr(port, "close"):
                port.close()
                print("[SquatCountingSystem] Board 串口 port 已关闭")
        except Exception as e:
            print(f"[SquatCountingSystem] 关闭 Board 串口失败: {e}")

    @staticmethod
    def _start_board_reception_if_needed(board):
        """
        如果某个功能需要天问模块，需要开启 Board 接收线程。
        当前深蹲计数默认不调用。
        """
        if board is None:
            return

        try:
            if hasattr(board, "enable_reception"):
                board.enable_reception()
                print("[SquatCountingSystem] Board 接收线程已启动")
        except Exception as e:
            print(f"[SquatCountingSystem] Board 接收线程启动失败: {e}")

    @staticmethod
    def _read_wakeup_level(board):
        """
        读取天问模块唤醒电平。
        当前深蹲计数默认不调用。
        """
        if board is None:
            return None

        try:
            if hasattr(board, "get_wkup"):
                return board.get_wkup()
        except Exception:
            return None

        return None

    # ============================================================
    # 工具方法
    # ============================================================

    @staticmethod
    def _safe_speak(text: str, speaker_module=None):
        print(text)

        if speaker_module is not None:
            try:
                speaker_module.speak(text)
            except Exception:
                pass

    @staticmethod
    def _read_count_file(count_file_path: str) -> int:
        count = 0

        if os.path.exists(count_file_path):
            try:
                with open(count_file_path, "r") as f:
                    txt = f.read().strip()

                if txt.isdigit():
                    count = int(txt)
            except Exception:
                pass

        return count

    @staticmethod
    def _write_count_file(count_file_path: str, count_value: int):
        try:
            with open(count_file_path, "w") as f:
                f.write(str(int(count_value)))
        except OSError:
            pass

    # ============================================================
    # 后台任务：深蹲计数
    # ============================================================

    @staticmethod
    def background_counting_task(
        video_source,
        count_file_path,
        pid_file_path,
        tts_mp_q=None,
        need_board: bool = False,
        start_gate_path=None,
        initial_count: int = 0,
        resume_from_interrupt: bool = False,
        initial_elapsed_seconds: float = 0.0,
    ):
        """
        后台深蹲计数任务。

        need_board:
            False：默认不初始化 Board。
            True ：如果该功能需要底轮、步进电机或天问模块，则在任务开始时初始化 Board，
                   并在 finally 中释放。
        """
        is_running = True
        interrupted_by_signal = False

        def handle_sigterm(signum, frame_obj):
            nonlocal is_running, interrupted_by_signal
            is_running = False
            interrupted_by_signal = True

        signal.signal(signal.SIGTERM, handle_sigterm)
        signal.signal(signal.SIGINT, handle_sigterm)

        board = None
        board_owned = False

        cap = None
        det = None
        det_owned = False

        try:
            import speaker

            try:
                speaker.init_mp_queue(tts_mp_q)
            except Exception:
                pass

        except Exception:
            speaker = None

        try:
            if need_board:
                board = SquatCountingSystem._create_board()
                board_owned = not isinstance(board, SquatCountingSystem.DummyBoard)

                # 如果后续需要天问模块，取消下面这一行的注释。
                # SquatCountingSystem._start_board_reception_if_needed(board)

            det, det_owned = runtime_models.acquire_or_create(
                "squat_detector",
                SquatCountingSystem.SquatDet,
                description="深蹲 MediaPipe 姿态检测器",
            )

            cap = SquatCountingSystem.CameraReader(video_source)

            repetition_counter = counter.RepetitionCounter(
                class_name="squat_down",
                enter_threshold=5,
                exit_threshold=4,
            )
            single_subject_gate = SquatCountingSystem.SingleSubjectGate()

            initial_count = max(0, int(initial_count or 0))
            initial_elapsed_seconds = max(0.0, float(initial_elapsed_seconds or 0.0))
            SquatCountingSystem._write_count_file(count_file_path, initial_count)

            if start_gate_path:
                _single_function_emit_ready("squat", "请准备好，1，2，3，开始")
                if not _single_function_wait_start_gate(start_gate_path, lambda: not is_running):
                    return

            if resume_from_interrupt and initial_count > 0:
                SquatCountingSystem._safe_speak(
                    f"继续深蹲计数，之前已经完成{number_to_chinese(initial_count)}个。",
                    speaker_module=speaker,
                )

            SquatCountingSystem._safe_speak(
                f"深蹲计数已开始，时长{number_to_chinese(SquatCountingSystem.SESSION_SECONDS)}秒。",
                speaker_module=speaker,
            )

            invalid_frame_count = 0
            last_session_count = 0
            last_count = initial_count

            session_start = time.monotonic()
            last_motion_time = session_start
            _single_function_emit_progress(
                "squat",
                state="active",
                initial_count=initial_count,
                session_count=0,
                current_count=last_count,
                count=last_count,
                elapsed_seconds=round(initial_elapsed_seconds, 2),
                resume_from_interrupt=bool(resume_from_interrupt),
            )

            while cap.isOpened() and is_running:
                now = time.monotonic()
                if not is_running:
                    break

                if now - session_start >= SquatCountingSystem.SESSION_SECONDS:
                    SquatCountingSystem._safe_speak(
                        f"{number_to_chinese(SquatCountingSystem.SESSION_SECONDS)}秒已到，本次深蹲计数结束，共做了{number_to_chinese(last_count)}个。",
                        speaker_module=speaker,
                    )
                    break

                if now - last_motion_time >= SquatCountingSystem.IDLE_SECONDS:
                    SquatCountingSystem._safe_speak(
                        f"连续十秒没有检测到新动作，本次深蹲计数自动结束，共做了{number_to_chinese(last_count)}个。",
                        speaker_module=speaker,
                    )
                    break

                ret, frame = cap.read()

                if not ret or not _is_valid_bgr_frame(frame):
                    invalid_frame_count += 1

                    if invalid_frame_count % 50 == 0:
                        print(f"[警告] 收到无效帧，shape={getattr(frame, 'shape', None)}")

                    time.sleep(0.01)
                    continue

                try:
                    pose_classification = det.infer(frame)
                except Exception as e:
                    print(f"[推理异常] {e}, shape={getattr(frame, 'shape', None)}")
                    continue

                if not pose_classification:
                    continue

                if not single_subject_gate.accept(det.last_result):
                    continue

                squat_count = repetition_counter(pose_classification)
                current_total_count = initial_count + squat_count

                if squat_count > last_session_count:
                    for i in range(last_count + 1, current_total_count + 1):
                        chinese_number = number_to_chinese(i)
                        print(chinese_number)

                        SquatCountingSystem._safe_speak(
                            f"第{chinese_number}个",
                            speaker_module=speaker,
                        )

                    last_session_count = squat_count
                    last_count = current_total_count
                    last_motion_time = now
                    _single_function_emit_progress(
                        "squat",
                        state="active",
                        initial_count=initial_count,
                        session_count=last_session_count,
                        current_count=last_count,
                        count=last_count,
                        elapsed_seconds=round(initial_elapsed_seconds + now - session_start, 2),
                        resume_from_interrupt=bool(resume_from_interrupt),
                    )

                    SquatCountingSystem._write_count_file(
                        count_file_path,
                        last_count,
                    )

                    print(f"-> 完成了一个深蹲！当前总数: {last_count}")

            if interrupted_by_signal:
                _single_function_emit_interrupted(
                    "squat",
                    state="interrupted",
                    initial_count=initial_count,
                    session_count=last_session_count,
                    current_count=last_count,
                    count=last_count,
                    elapsed_seconds=round(initial_elapsed_seconds + time.monotonic() - session_start, 2),
                    resume_from_interrupt=bool(resume_from_interrupt),
                )

        except Exception as e:
            print(f"[深蹲计数异常] {e}")

        finally:
            if det is not None and det_owned:
                try:
                    det.release()
                except Exception:
                    pass

            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

            if board_owned:
                SquatCountingSystem._release_board(board)
                board = None

            _safe_remove_file(pid_file_path)

            print("\n[深蹲进程] 已退出，资源已安全释放")

    # ============================================================
    # 对外主调用方法
    # ============================================================

    def start_squat_counting(self, video_source, need_board: bool = False, start_gate_path=None, initial_count: int = 0, resume_from_interrupt: bool = False, initial_elapsed_seconds: float = 0.0):
        """
        开始深蹲计数。

        默认 need_board=False，不初始化 Board。
        如果未来这个功能需要轮子、步进电机或天问模块，再传 need_board=True。
        """
        if self._process is not None and self._process.is_alive():
            try:
                import speaker

                speaker.speak("计数任务已经在后台运行中")
            except Exception:
                print("计数任务已经在后台运行中")

            return False

        _safe_remove_file(self.pid_file)

        ctx = runtime_config.get_mp_context()

        p = ctx.Process(
            target=self.__class__.background_counting_task,
            args=(
                video_source,
                self.count_file,
                self.pid_file,
                None,
                need_board,
                start_gate_path,
                initial_count,
                resume_from_interrupt,
                initial_elapsed_seconds,
            ),
            daemon=True,
        )

        p.start()
        self._process = p

        try:
            with open(self.pid_file, "w") as f:
                f.write(str(p.pid))
        except OSError as e:
            print(f"写入 PID 文件失败: {e}")

        print(
            f"深蹲计数已在后台启动，PID={p.pid}，"
            f"最多 {self.SESSION_SECONDS} 秒；"
            f"连续 {self.IDLE_SECONDS} 秒无动作会自动结束。"
        )

        return True

    def query_squat_count(self) -> int:
        """
        查询当前深蹲个数。

        返回：
            当前计数 int
        """
        count = self.__class__._read_count_file(self.count_file)

        print(f"\n[查询结果] 您目前做了 {count} 个深蹲了")

        return count

    def stop_squat_counting(self) -> int:
        """
        结束深蹲计数并汇总。

        返回：
            final_count: 最终个数
        """
        final_count = self.__class__._read_count_file(self.count_file)

        try:
            self._terminate_process()
        finally:
            _safe_remove_file(self.pid_file)
            _safe_remove_file(self.count_file)

        print(f"\n[汇总统计] 深蹲计数程序结束，您一共做了 {final_count} 个深蹲")

        return final_count

    # ============================================================
    # 兼容旧主程序的方法名
    # ============================================================

    def start_counting(self, video_source, start_gate_path=None, initial_count: int = 0, resume_from_interrupt: bool = False, initial_elapsed_seconds: float = 0.0):
        """
        兼容旧代码：
            squat_sys.start_counting(video_source)
        """
        return self.start_squat_counting(video_source, need_board=False, start_gate_path=start_gate_path, initial_count=initial_count, resume_from_interrupt=resume_from_interrupt, initial_elapsed_seconds=initial_elapsed_seconds)

    def query_progress(self):
        """
        兼容旧代码：
            squat_sys.query_progress()
        """
        return self.query_squat_count()

    def stop_and_summarize(self):
        """
        兼容旧代码：
            squat_sys.stop_and_summarize()
        """
        return self.stop_squat_counting()

    # ============================================================
    # 进程终止逻辑
    # ============================================================

    def _terminate_process(self):
        if self._process is not None:
            try:
                if self._process.is_alive():
                    print("\n发送 SIGTERM，等待 3s...")

                    self._process.terminate()
                    self._process.join(timeout=3.0)

                    if self._process.is_alive():
                        self._process.kill()
                        self._process.join(timeout=2.0)

                    print("进程彻底终止" if not self._process.is_alive() else "进程仍然存活")
                else:
                    print("\n进程已自行退出")

            except Exception as e:
                print(f"终止进程时出错: {e}")

            finally:
                self._process = None

            return

        if not os.path.exists(self.pid_file):
            return

        try:
            with open(self.pid_file, "r") as f:
                pid_str = f.read().strip()

            if not pid_str.isdigit():
                return

            pid = int(pid_str)

            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(3.0)

                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            except PermissionError:
                print("无权限终止该进程")
        except Exception as e:
            print(f"通过 PID 文件终止时出错: {e}")
    
if False and __name__ == "__main__":
    CAMERA_ID = DEFAULT_CAMERA
    squat_sys = SquatCountingSystem()

    print("\n" + "=" * 40)
    print(" [s]=开始  [q]=查询  [x]=停止汇总  [e]=退出")
    print("=" * 40)

    while True:
        cmd = input("\n请输入指令 (s/q/x/e): ").strip().lower()

        if cmd == "e":
            squat_sys.stop_squat_counting()
            break

        elif cmd == "s":
            squat_sys.start_squat_counting(CAMERA_ID)

        elif cmd == "q":
            squat_sys.query_squat_count()

        elif cmd == "x":
            squat_sys.stop_squat_counting()

        else:
            print("无效指令")
__file__ = _run_file
_publish_namespace('movement_count_2.squat_camera_base_fps_1', _before_inline, top_level_aliases=())
del _before_inline


def _single_function_emit_ready(skill_name, text):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import json as _json
    print(_json.dumps({
        "event": "skill_ready",
        "skill_name": skill_name,
        "kind": "ready",
        "text": text,
    }, ensure_ascii=False), flush=True)


def _single_function_wait_start_gate(start_gate_path, should_stop=None):
    if not start_gate_path:
        return True
    gate = Path(start_gate_path)
    while True:
        if gate.exists():
            return True
        if callable(should_stop) and should_stop():
            return False
        time.sleep(0.05)


def _single_function_emit_progress(skill_name, **payload):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import json as _json
    data = {"event": "skill_progress", "skill_name": skill_name, "kind": "progress"}
    data.update(payload)
    print(_json.dumps(data, ensure_ascii=False), flush=True)


def _single_function_emit_interrupted(skill_name, **payload):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import json as _json
    data = {"event": "skill_interrupted", "skill_name": skill_name, "kind": "interrupted"}
    data.update(payload)
    print(_json.dumps(data, ensure_ascii=False), flush=True)

# ===== Skill entrypoint =====
def main():
    import argparse
    import time
    from movement_count_2.squat_camera_base_fps_1 import SquatCountingSystem

    parser = argparse.ArgumentParser(description='Squat' + ' counting skill.')
    parser.add_argument('action', nargs='?', default='run', choices=['start', 'query', 'stop', 'run'])
    parser.add_argument('--camera', default=None)
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--start-gate', default=None)
    parser.add_argument('--initial-count', type=int, default=0)
    parser.add_argument('--resume-from-interrupt', action='store_true')
    parser.add_argument('--initial-elapsed-seconds', type=float, default=0.0)
    args = parser.parse_args()

    duration = max(1, int(args.duration))
    SquatCountingSystem.SESSION_SECONDS = duration

    def wait_for_process_or_timeout(max_seconds):
        process = getattr(system, "_process", None)
        if process is None:
            time.sleep(max_seconds)
            return

        deadline = time.monotonic() + max_seconds
        while process.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            process.join(timeout=min(0.2, remaining))

    system = SquatCountingSystem()
    if args.action == 'start':
        started = system.start_counting(args.camera or DEFAULT_CAMERA, start_gate_path=args.start_gate, initial_count=args.initial_count, resume_from_interrupt=args.resume_from_interrupt, initial_elapsed_seconds=args.initial_elapsed_seconds)
        print(started, flush=True)
        if started and getattr(system, "_process", None) is not None:
            try:
                system._process.join()
            except KeyboardInterrupt:
                system.stop_and_summarize()
    elif args.action == 'query':
        print(system.query_progress())
    elif args.action == 'stop':
        print(system.stop_and_summarize())
    else:
        started = system.start_counting(args.camera or DEFAULT_CAMERA, start_gate_path=args.start_gate, initial_count=args.initial_count, resume_from_interrupt=args.resume_from_interrupt, initial_elapsed_seconds=args.initial_elapsed_seconds)
        if started:
            wait_for_process_or_timeout(duration)
        print(system.stop_and_summarize())


if __name__ == '__main__':
    main()
