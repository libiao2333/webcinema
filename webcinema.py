import argparse
import os
import mimetypes
import time
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import threading
import logging
import shutil

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 添加额外的视频 MIME 类型映射
mimetypes.add_type("video/x-flv", ".flv")
mimetypes.add_type("video/vnd.rn-realvideo", ".rm")
mimetypes.add_type("video/vnd.rn-realvideo", ".rmvb")
mimetypes.add_type("video/x-f4v", ".f4v")
mimetypes.add_type("video/x-ms-vob", ".vob")
mimetypes.add_type("video/divx", ".divx")
mimetypes.add_type("video/3gpp", ".3gs")
mimetypes.add_type("video/3gpp", ".3gp")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/x-msvideo", ".avi")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/mpeg", ".mpeg")
mimetypes.add_type("video/mpeg", ".mpg")

try:
    from deffcode import FFdecoder
    DEFFCODE_AVAILABLE = True
    logger.info("DeFFcode 库已成功导入，支持硬件加速解码")
except ImportError:
    DEFFCODE_AVAILABLE = False
    logger.warning("DeFFcode 库未安装，将使用传统解码方式")

from flask import Flask, request, send_file, abort, Response, render_template, redirect, url_for, jsonify

app = Flask(__name__, template_folder="templates")

# 配置
app.config['MAX_THUMBNAIL_SIZE'] = (320, 240)  # 缩略图尺寸
app.config['IMAGE_CACHE_DIR'] = '.webcinema_cache'
app.config['USE_HARDWARE_ACCEL'] = True  # 是否使用硬件加速
app.config['GPU_DEVICE'] = 0  # GPU设备索引

# 自定义 Jinja2 过滤器：将整数时间戳格式化为可读日期时间
def datetime_filter(timestamp):
    """将整数时间戳转换为可读日期时间字符串"""
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, OSError):
        return str(timestamp)

app.jinja_env.filters['datetime'] = datetime_filter

def get_ffmpeg_path():
    """返回 FFmpeg 可执行文件的路径，优先使用项目内捆绑的版本"""
    import sys
    import os
    import platform

    # 可能的路径列表（优先级从高到低）
    candidates = []

    # 1. 项目根目录下的 bin 子目录（适用于打包分发）
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后的可执行文件所在目录
        base_dir = sys._MEIPASS if hasattr(sys, '_MEIPASS') else os.path.dirname(sys.executable)
    else:
        # 开发环境：当前脚本所在目录
        base_dir = os.path.dirname(os.path.abspath(__file__))

    # 根据不同平台构造可执行文件名
    exe_name = 'ffmpeg.exe' if platform.system() == 'Windows' else 'ffmpeg'

    # 1. 项目下的 bin 目录
    bin_path = os.path.join(base_dir, 'bin', exe_name)
    candidates.append(bin_path)

    # 2. 项目根目录下的 ffmpeg 子目录（旧版兼容）
    bundled_path = os.path.join(base_dir, 'ffmpeg', exe_name)
    candidates.append(bundled_path)

    # 3. 直接放在项目根目录下的可执行文件
    candidates.append(os.path.join(base_dir, exe_name))

    # 4. 系统 PATH 中的 ffmpeg
    candidates.append('ffmpeg')

    for path in candidates:
        if path == 'ffmpeg':
            # 依赖 shutil.which 查找系统路径
            import shutil
            if shutil.which(path):
                return path
        else:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path

    # 未找到任何可用的 ffmpeg
    return None

def natural_sort_key(filename):
    """
    用于自然排序的函数。
    将文件名分解为文本和数字部分，数字部分转换为整数以便正确排序。
    例如: "航 (1).jpg" < "航 (2).jpg" < "航 (10).jpg" < "航 (100).jpg"
    """
    import re
    # 将文件名分解为文本和数字混合的列表
    # 例如 "航 (123).jpg" -> ['航 (', 123, ').jpg']
    parts = []
    for part in re.split(r'(\d+)', str(filename).lower()):
        if part.isdigit():
            parts.append((0, int(part)))  # 数字排在前面
        else:
            parts.append((1, part))  # 文本排在后面
    return parts

def detect_hardware_acceleration():
    """检测可用的硬件加速器与 GPU 编码器"""
    import subprocess
    
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        logger.warning("未找到 FFmpeg 可执行文件，无法检测硬件加速")
        return {
            'hwaccels': [],
            'gpu_encoders': [],
            'gpu_type': None,
            'has_cuda': False,
            'has_qsv': False,
            'has_amf': False,
        }
    
    hwaccels = []
    gpu_encoders = []
    verified_encoders = []
    gpu_type = None  # 检测到的GPU类型：intel, amd, nvidia
    
    try:
        # 检测硬件加速方法
        result = subprocess.run([ffmpeg_path, '-hwaccels'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            in_hwaccel_section = False
            for line in lines:
                line = line.strip()
                if 'Hardware acceleration methods:' in line:
                    in_hwaccel_section = True
                    continue
                if in_hwaccel_section and line:
                    hwaccels.append(line)
        
        # 检测可用的 GPU 编码器
        result = subprocess.run([ffmpeg_path, '-encoders'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            encoder_list = {}
            has_nvidia = False
            has_amd = False
            has_intel = False
            
            for line in lines:
                # 格式: " V....D h264_nvenc      NVIDIA NVENC H.264 encoder..."
                if 'h264_nvenc' in line:
                    encoder_list['h264_nvenc'] = 'cuda'
                    has_nvidia = True
                elif 'hevc_nvenc' in line:
                    encoder_list['hevc_nvenc'] = 'cuda'
                    has_nvidia = True
                elif 'av1_nvenc' in line:
                    encoder_list['av1_nvenc'] = 'cuda'
                    has_nvidia = True
                elif 'h264_qsv' in line:
                    encoder_list['h264_qsv'] = 'qsv'
                    has_intel = True
                elif 'hevc_qsv' in line:
                    encoder_list['hevc_qsv'] = 'qsv'
                    has_intel = True
                elif 'h264_amf' in line:
                    encoder_list['h264_amf'] = 'amf'
                    has_amd = True
                elif 'hevc_amf' in line:
                    encoder_list['hevc_amf'] = 'amf'
                    has_amd = True
            
            # 确定GPU类型（优先级：Intel QSV > AMD AMF > NVIDIA NVENC）
            if has_intel:
                gpu_type = 'intel'
            elif has_amd:
                gpu_type = 'amd'
            elif has_nvidia:
                gpu_type = 'nvidia'
            
            # 根据检测到的GPU类型，只测试相关的编码器
            if gpu_type == 'intel':
                priority_order = ['h264_qsv', 'hevc_qsv']
                logger.info("检测到 Intel 显卡，将测试 QSV 编码器")
            elif gpu_type == 'amd':
                priority_order = ['h264_amf', 'hevc_amf']
                logger.info("检测到 AMD 显卡，将测试 AMF 编码器")
            elif gpu_type == 'nvidia':
                priority_order = ['h264_nvenc', 'hevc_nvenc', 'av1_nvenc']
                logger.info("检测到 NVIDIA 显卡，将测试 NVENC 编码器")
            else:
                priority_order = []
            
            for encoder in priority_order:
                if encoder in encoder_list and encoder not in verified_encoders:
                    # 验证编码器是否真的可用（快速测试）
                    if _verify_encoder(encoder, encoder_list[encoder]):
                        gpu_encoders.append((encoder, encoder_list[encoder]))
                        verified_encoders.append(encoder)
                        logger.info(f"✓ 找到可用的 {gpu_type.upper()} GPU编码器: {encoder}")
                    break
            
            # 如果还没有找到编码器
            if not gpu_encoders:
                if gpu_type:
                    logger.warning(f"⚠ 检测到 {gpu_type.upper()} 显卡，但未找到可用的GPU编码器。将使用CPU软件编码。")
                else:
                    logger.warning(f"⚠ 系统中未检测到可用的GPU编码器。将使用CPU软件编码。")
        
    except Exception as e:
        logger.warning(f"检测硬件加速失败: {e}")
    
    return {
        'hwaccels': hwaccels,
        'gpu_encoders': gpu_encoders,
        'gpu_type': gpu_type,
        'has_cuda': 'cuda' in hwaccels,
        'has_qsv': 'qsv' in hwaccels,
        'has_amf': 'amf' in hwaccels or 'dxva2' in hwaccels,
    }


def _verify_encoder(encoder_name, hwaccel):
    """通过快速测试验证硬件编码器是否真的可用"""
    import subprocess
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        logger.warning("未找到 FFmpeg 可执行文件，无法验证编码器")
        return False
    try:
        # 为不同的编码器和硬件加速选择合适的参数
        cmd = [ffmpeg_path, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=s=320x240:d=1']
        
        # 某些编码器需要在输入前指定硬件加速
        if hwaccel == 'qsv':
            # Intel QSV 需要在输入前指定硬件加速
            cmd = [ffmpeg_path, '-hide_banner', '-loglevel', 'error', '-hwaccel', hwaccel, '-f', 'lavfi', '-i', 'testsrc=s=320x240:d=1']
        elif hwaccel == 'amf':
            # AMD AMF 也需要硬件加速支持
            cmd = [ffmpeg_path, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=s=320x240:d=1']
        
        cmd.extend(['-c:v', encoder_name, '-t', '0.1', '-f', 'null', '-'])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        success = result.returncode == 0
        
        if success:
            logger.info(f"✓ 硬件编码器验证成功: {encoder_name} ({hwaccel})")
        else:
            err_msg = result.stderr[:200] if result.stderr else result.stdout[:200]
            logger.debug(f"硬件编码器验证失败: {encoder_name} ({hwaccel}), 错误: {err_msg}")
        
        return success
    except subprocess.TimeoutExpired:
        logger.debug(f"硬件编码器验证超时: {encoder_name} ({hwaccel})")
        return False
    except Exception as e:
        logger.debug(f"硬件编码器验证异常: {encoder_name} ({hwaccel}), {e}")
        return False


def ffmpeg_available():
        """检查系统是否可以调用 `ffmpeg` 可执行文件"""
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            return False
        try:
                import subprocess
                res = subprocess.run([ffmpeg_path, '-version'], capture_output=True, text=True, timeout=3)
                return res.returncode == 0
        except Exception:
                return False


FFMPEG_INSTALL_GUIDE = '''
如果未安装 FFmpeg，可按下列方法在 Windows 上安装：

- 使用 winget (Windows 10/11):
    winget install -e --id Gyan.FFmpeg

- 使用 Chocolatey:
    choco install ffmpeg -y

- 使用 Scoop:
    scoop install ffmpeg

- 或者从 https://www.gyan.dev/ffmpeg/builds/ 或 https://ffmpeg.org 下载预编译的静态包，解压后把包含 ffmpeg.exe 的目录添加到系统 PATH。

安装完成后运行 `ffmpeg -version` 验证安装，运行 `ffmpeg -hwaccels` 查看可用硬件加速器。
更多关于 GPU 编码（如 NVENC、QSV、AMF）的要求：
- NVENC: 需要 NVIDIA 驱动并且 FFmpeg 要包含 nvenc 支持（大多数 gyan/chocolatey 构建包含）。
- QSV: 需要 Intel Media SDK / 支持的驱动。
- AMF: 需要 AMD 驱动和支持的 FFmpeg 构建。
'''

# 创建缓存目录
os.makedirs(app.config['IMAGE_CACHE_DIR'], exist_ok=True)

def safe_path(root, relpath):
    """安全路径检查"""
    full = os.path.realpath(os.path.join(root, relpath))
    root_real = os.path.realpath(root)
    if os.path.commonprefix([full, root_real]) != root_real:
        return None
    return full

def _get_directory_cache_key_raw(root, relpath):
    """生成目录缓存键"""
    full = safe_path(root, relpath)
    if not full or not os.path.exists(full):
        return None
    try:
        stat = os.stat(full)
        return f"{full}:{stat.st_mtime}"
    except:
        return None

def _list_dir_entries_cached_raw(root, relpath, cache_key):
    """带缓存的目录列表（使用更高效的排序）"""
    full = safe_path(root, relpath)
    if full is None or not os.path.exists(full):
        return []
    
    entries = []
    try:
        with os.scandir(full) as it:
            for entry in it:
                try:
                    rel_path = os.path.join(relpath, entry.name).replace("\\", "/")
                    entries.append({
                        "name": entry.name,
                        "relpath": rel_path,
                        "is_dir": entry.is_dir(),
                        "size": entry.stat().st_size if not entry.is_dir() else 0,
                        "modified": entry.stat().st_mtime
                    })
                except PermissionError:
                    continue
    except PermissionError:
        return []
    
    # 优化排序：先目录后文件，按名称排序（使用自然排序支持数字）
    entries.sort(key=lambda x: (
        not x['is_dir'],  # 目录在前
        natural_sort_key(x['name'])  # 自然排序（正确处理文件名中的数字）
    ))
    
    return entries

get_directory_cache_key = lru_cache(maxsize=256)(_get_directory_cache_key_raw)
list_dir_entries_cached = lru_cache(maxsize=256)(_list_dir_entries_cached_raw)

def list_dir_entries(root, relpath=""):
    """获取目录条目（带智能缓存）"""
    cache_key = get_directory_cache_key(root, relpath)
    if cache_key is None:
        return []
    
    return list_dir_entries_cached(root, relpath, cache_key)

def get_media_info(file_path):
    """获取媒体文件信息（使用DeFFcode）"""
    if not DEFFCODE_AVAILABLE or not os.path.exists(file_path):
        return {}
    
    try:
        decoder_config = {
            "source": file_path,
            "verbose": False,
            "frame_format": "rgb24"  # 使用有效的像素格式而不是 'null'
        }
        
        with FFdecoder(**decoder_config) as decoder:
            info = decoder.metadata
            # 如果 info 是字符串，尝试解析为 JSON
            if isinstance(info, str):
                try:
                    import json
                    info = json.loads(info)
                except json.JSONDecodeError:
                    logger.warning(f"metadata 是字符串但非 JSON: {info[:100]}")
                    return {}
            # 确保 info 是字典
            if not isinstance(info, dict):
                logger.warning(f"metadata 不是字典，而是 {type(info)}: {info}")
                return {}
            # 从解析后的 metadata 提取字段
            duration = info.get('source_duration_sec', 0)
            resolution = info.get('source_video_resolution', [0, 0])
            width = resolution[0] if isinstance(resolution, list) and len(resolution) >= 2 else 0
            height = resolution[1] if isinstance(resolution, list) and len(resolution) >= 2 else 0
            fps = info.get('source_video_framerate', 0)
            codec = info.get('source_video_decoder', '未知')
            bitrate_str = info.get('source_video_bitrate', '0')
            format_ = info.get('source_extension', '未知')
            
            # 解析比特率字符串（如 "727k" -> 727000）
            bitrate = 0
            if bitrate_str:
                try:
                    # 移除空格，处理后缀 k/M
                    bitrate_str = bitrate_str.strip().lower()
                    multiplier = 1
                    if bitrate_str.endswith('k'):
                        multiplier = 1000
                        bitrate_str = bitrate_str[:-1]
                    elif bitrate_str.endswith('m'):
                        multiplier = 1000000
                        bitrate_str = bitrate_str[:-1]
                    # 转换为浮点数后乘以倍数
                    bitrate = int(float(bitrate_str) * multiplier)
                except (ValueError, TypeError):
                    bitrate = 0
            
            # 格式化时长
            if duration:
                hours = int(duration // 3600)
                minutes = int((duration % 3600) // 60)
                seconds = int(duration % 60)
                duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
            else:
                duration_str = "未知"
            
            return {
                'duration': duration,
                'duration_str': duration_str,
                'width': width,
                'height': height,
                'fps': fps,
                'codec': codec,
                'bitrate': bitrate,
                'format': format_
            }
    except Exception as e:
        logger.error(f"获取媒体信息失败: {e}")
        return {}

def compute_file_hash(file_path):
    """计算文件的 SHA256 哈希值"""
    import hashlib
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # 分块读取以避免大文件内存问题
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_reading_progress(file_hash):
    """从缓存中读取阅读进度"""
    import json
    progress_file = os.path.join(app.config['IMAGE_CACHE_DIR'], 'reading_progress.json')
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get(file_hash)
        except (json.JSONDecodeError, IOError):
            pass
    return None

def save_reading_progress(file_hash, progress_data):
    """保存阅读进度到缓存"""
    import json
    progress_file = os.path.join(app.config['IMAGE_CACHE_DIR'], 'reading_progress.json')
    data = {}
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    data[file_hash] = {
        'position': progress_data.get('position'),
        'percentage': progress_data.get('percentage'),
        'timestamp': time.time()
    }
    try:
        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except IOError:
        logger.error(f"无法写入进度文件 {progress_file}")

@app.route("/")
def index():
    """主页 - 文件浏览器"""
    rel = request.args.get("path", "")
    entries = list_dir_entries(app.config["ROOT_DIR"], rel)
    
    # 构建面包屑导航
    parts = [p for p in rel.split("/") if p]
    crumbs = []
    accum = []
    for p in parts:
        accum.append(p)
        crumbs.append((p, "/?path=" + "/".join(accum)))
    
    return render_template("index.html", 
                         entries=entries, 
                         crumbs=crumbs, 
                         curpath=rel)

@app.route("/view/<path:subpath>")
def view_file(subpath):
    """查看文件"""
    full = safe_path(app.config["ROOT_DIR"], subpath)
    if full is None or not os.path.exists(full):
        abort(404)
    
    mime, _ = mimetypes.guess_type(full)
    filename = os.path.basename(full)
    
    # 处理图片（图库模式）
    if mime and mime.startswith("image"):
        # 获取同目录下所有图片
        dir_path = os.path.dirname(subpath)
        entries = list_dir_entries(app.config["ROOT_DIR"], dir_path)
        
        image_files = []
        for entry in entries:
            if not entry['is_dir']:
                file_path = safe_path(app.config["ROOT_DIR"], entry['relpath'])
                file_mime, _ = mimetypes.guess_type(file_path)
                if file_mime and file_mime.startswith("image"):
                    image_files.append(entry['relpath'])
        
        # 按文件名排序（使用自然排序支持数字）
        image_files.sort(key=lambda x: natural_sort_key(os.path.basename(x)))
        
        # 查找当前图片索引
        current_index = -1
        try:
            current_index = image_files.index(subpath)
        except ValueError:
            pass
        
        # 计算上一张和下一张
        prev_url = None
        next_url = None
        if current_index > 0:
            prev_url = url_for("view_file", subpath=image_files[current_index - 1])
        if current_index < len(image_files) - 1:
            next_url = url_for("view_file", subpath=image_files[current_index + 1])
        
        return render_template("viewer.html",
                             src_url=url_for("files_raw", subpath=subpath),
                             mime=mime,
                             name=filename,
                             prev_url=prev_url,
                             next_url=next_url,
                             image_list=image_files,
                             current_index=current_index)
    
    # 处理视频/音频
    elif mime and mime.startswith(('video', 'audio')):
        # 检查格式兼容性
        file_ext = os.path.splitext(full)[1].lower()
        # 浏览器原生支持的格式（注：AVI虽然有些浏览器支持，但兼容性差，建议转码）
        natively_supported = ['.mp4', '.webm', '.ogg', '.ogv', '.m4v']
        # 如果格式不被原生支持，则需要转码
        needs_transcode = file_ext not in natively_supported
        
        # 只有在需要转码或需要显示详细信息时才获取媒体信息（以节省资源）
        media_info = {}
        if needs_transcode or mime.startswith('audio'):
            # 对于需要转码的格式或音频文件，获取详细信息
            media_info = get_media_info(full)
        
        # 检查是否支持硬件加速
        supports_hardware = DEFFCODE_AVAILABLE
        
        # 根据是否需要转码，选择合适的URL
        if needs_transcode:
            src_url = url_for("transcode_file", subpath=subpath)
        else:
            src_url = url_for("stream_file", subpath=subpath)
        
        return render_template("viewer.html",
                             src_url=src_url,
                             mime=mime if not needs_transcode else 'video/mp4',
                             name=filename,
                             needs_transcode=needs_transcode,
                             supports_hardware=supports_hardware,
                             media_info=media_info,
                             file_ext=file_ext,
                             subpath=subpath)
    
    # 处理文本文件
    elif mime and (mime.startswith('text') or filename.lower().endswith('.txt')):
        # 计算文件哈希
        file_hash = compute_file_hash(full)
        # 获取阅读进度
        progress = get_reading_progress(file_hash)
        # 读取文件内容（限制大小）
        max_size = 10 * 1024 * 1024  # 10 MB
        file_size = os.path.getsize(full)
        if file_size > max_size:
            # 文件过大，提供下载
            return redirect(url_for("files_raw", subpath=subpath))
        # 尝试多种编码读取文本文件
        encodings = ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'big5', 'shift_jis', 'latin-1']
        content = None
        for enc in encodings:
            try:
                with open(full, 'r', encoding=enc, errors='strict') as f:
                    content = f.read()
                    break
            except (UnicodeDecodeError, LookupError):
                continue
        if content is None:
            # 所有编码尝试失败，使用 errors='replace' 作为最后手段
            with open(full, 'r', errors='replace') as f:
                content = f.read()
        # 渲染文本阅读器模板
        return render_template('text_viewer.html',
                               content=content,
                               name=filename,
                               subpath=subpath,
                               file_hash=file_hash,
                               progress=progress,
                               file_size=file_size)

    # 其他文件类型
    else:
        return redirect(url_for("files_raw", subpath=subpath))

@app.route("/transcode/<path:subpath>")
def transcode_file(subpath):
    """转码文件流（用于不兼容格式）"""
    import tempfile
    import subprocess
    
    # 本路由依赖系统上安装的 FFmpeg 来进行转码（支持软/硬件加速）
    if not ffmpeg_available():
        abort(503, description=("系统未检测到 FFmpeg，可执行文件不可用。\n" + FFMPEG_INSTALL_GUIDE))

    full = safe_path(app.config["ROOT_DIR"], subpath)
    if full is None or not os.path.exists(full):
        abort(404)

    # 如果是图片文件，直接返回原始文件，不转码
    mime, _ = mimetypes.guess_type(full)
    if mime and mime.startswith("image"):
        logger.info(f"图片文件请求到转码路由，直接返回原始文件: {subpath}")
        return send_file(full, mimetype=mime)

    # 创建临时MP4文件用于转码输出
    temp_file = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
    temp_path = temp_file.name
    temp_file.close()

    def cleanup_temp():
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except:
            pass

    try:
        # 检测硬件加速信息
        hw_info = detect_hardware_acceleration()
        use_hw = app.config['USE_HARDWARE_ACCEL'] and bool(hw_info.get('gpu_encoders'))

        # 首先尝试使用硬件加速，如果失败则回退到软件编码
        attempts = [
            ('hardware', use_hw),  # 首次尝试：硬件加速
            ('software', True)     # 备选方案：软件编码
        ]

        transcode_success = False
        last_error = None

        for attempt_type, should_attempt in attempts:
            if not should_attempt:
                continue

            logger.info(f"尝试转码方案: {attempt_type}")

            # 构建 FFmpeg 命令
            ffmpeg_path = get_ffmpeg_path()
            ffmpeg_cmd = [ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error']

            if attempt_type == 'hardware' and use_hw:
                encoder, hwaccel = hw_info['gpu_encoders'][0]
                logger.info(f"使用 GPU 加速：编码器={encoder}，硬件加速={hwaccel}")
                
                # 检查输入文件是否是JPEG（某些硬件加速对JPEG支持不好）
                file_ext = os.path.splitext(full)[1].lower()
                is_jpeg = file_ext in ['.jpg', '.jpeg', '.jpe']
                
                # 为不同的硬件加速类型应用不同的参数
                if hwaccel == 'qsv' and not is_jpeg:
                    # Intel Quick Sync Video - JPEG支持不稳定，跳过硬件解码
                    ffmpeg_cmd.extend(['-hwaccel', 'qsv'])
                elif hwaccel == 'amf':
                    # AMD Media Framework - 不需要解码硬件加速
                    pass
                elif hwaccel == 'cuda' and not is_jpeg:
                    # NVIDIA CUDA - JPEG也可能有问题
                    ffmpeg_cmd.extend(['-hwaccel', 'cuda', '-hwaccel_device', str(app.config['GPU_DEVICE'])])
            else:
                logger.info("使用软件编码（CPU）")

            # 输入文件
            ffmpeg_cmd.extend(['-i', full])

            # 视频编码
            if attempt_type == 'hardware' and use_hw:
                encoder, hwaccel = hw_info['gpu_encoders'][0]
                
                # 为不同的编码器设置合适的参数
                if encoder == 'h264_qsv':
                    # Intel QSV 编码器
                    ffmpeg_cmd.extend(['-c:v', encoder, '-preset', 'veryfast', '-b:v', '2500k', '-pix_fmt', 'yuv420p'])
                elif encoder == 'h264_amf':
                    # AMD AMF 编码器
                    ffmpeg_cmd.extend(['-c:v', encoder, '-quality', 'speed', '-b:v', '2500k', '-pix_fmt', 'yuv420p'])
                else:  # h264_nvenc
                    # NVIDIA NVENC 编码器
                    ffmpeg_cmd.extend(['-c:v', encoder, '-preset', 'fast', '-b:v', '2000k', '-maxrate', '3000k', '-bufsize', '4000k', '-pix_fmt', 'yuv420p'])
            else:
                # 软件编码使用超快速设置以加速转码
                ffmpeg_cmd.extend(['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28', '-pix_fmt', 'yuv420p'])

            # 音频编码
            ffmpeg_cmd.extend(['-c:a', 'aac', '-b:a', '128k', '-ac', '2'])

            # 输出为普通MP4文件（带faststart以支持边播边下载）
            ffmpeg_cmd.extend(['-f', 'mp4', '-movflags', 'faststart', temp_path])

            logger.info(f"FFmpeg 命令: {' '.join(ffmpeg_cmd)}")

            try:
                # 执行转码
                result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=600)

                if result.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 1000:
                    logger.info(f"转码成功（{attempt_type}方案），文件大小: {os.path.getsize(temp_path)} 字节")
                    transcode_success = True
                    break
                else:
                    err = result.stderr or result.stdout or "未知错误"
                    last_error = err
                    logger.warning(f"转码失败（{attempt_type}方案）: {err[:300]}")

            except subprocess.TimeoutExpired:
                logger.error(f"转码超时（{attempt_type}方案）")
                last_error = "转码超时"
            except Exception as e:
                logger.error(f"转码异常（{attempt_type}方案）: {e}")
                last_error = str(e)

        # 如果转码失败，返回错误
        if not transcode_success:
            cleanup_temp()
            error_msg = f"无法转码视频: {last_error[:300]}" if last_error else "转码失败"
            logger.error(error_msg)
            abort(500, description=error_msg)

        # 发送临时MP4文件
        file_size = os.path.getsize(temp_path)
        
        def generate():
            try:
                with open(temp_path, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                cleanup_temp()

        headers = {
            'Content-Type': 'video/mp4',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache'
        }
        
        return Response(generate(), headers=headers, mimetype='video/mp4')

    except Exception as e:
        cleanup_temp()
        logger.error(f"转码端点异常: {e}")
        abort(500, description=f"服务器错误: {str(e)}")

def partial_response(path, range_header):
    """部分响应（支持断点续传）"""
    full_size = os.path.getsize(path)
    if not range_header:
        return None
    
    units, _, range_spec = range_header.partition("=")
    if units != "bytes":
        return None
    
    start_str, _, end_str = range_spec.partition("-")
    try:
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else full_size - 1
    except ValueError:
        return None
    
    if start > end or end >= full_size:
        return None
    
    chunk_size = end - start + 1
    
    def generate():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = chunk_size
            while remaining > 0:
                read_len = min(8192, remaining)
                data = f.read(read_len)
                if not data:
                    break
                remaining -= len(data)
                yield data
    
    mime, _ = mimetypes.guess_type(path)
    rv = Response(generate(), status=206, mimetype=mime or "application/octet-stream")
    rv.headers["Content-Range"] = f"bytes {start}-{end}/{full_size}"
    rv.headers["Accept-Ranges"] = "bytes"
    rv.headers["Content-Length"] = str(chunk_size)
    return rv

@app.route("/files/<path:subpath>")
def files_raw(subpath):
    """原始文件访问"""
    full = safe_path(app.config["ROOT_DIR"], subpath)
    if full is None or not os.path.exists(full):
        abort(404)
    
    # 检查是否需要特殊处理
    mime, _ = mimetypes.guess_type(full)
    file_ext = os.path.splitext(full)[1].lower()
    
    # 浏览器原生支持的格式
    natively_supported = ['.mp4', '.webm', '.ogg', '.ogv', '.m4v', '.mpg', '.mpeg', '.avi', '.mov', '.wmv']
    # 如果格式不被原生支持且 DeFFcode 可用，则提供转码选项
    if file_ext not in natively_supported and DEFFCODE_AVAILABLE:
        return redirect(url_for("transcode_file", subpath=subpath))
    
    range_header = request.headers.get("Range", None)
    if range_header:
        part = partial_response(full, range_header)
        if part:
            return part
    
    return send_file(full, as_attachment=False, conditional=True)

@app.route("/stream/<path:subpath>")
def stream_file(subpath):
    """流媒体传输"""
    full = safe_path(app.config["ROOT_DIR"], subpath)
    if full is None or not os.path.exists(full):
        abort(404)
    
    # 获取正确的MIME类型
    mime, _ = mimetypes.guess_type(full)
    if not mime:
        mime = 'application/octet-stream'
    
    range_header = request.headers.get("Range", None)
    if range_header:
        part = partial_response(full, range_header)
        if part:
            return part
    
    # 设置响应头以支持视频流
    response = send_file(full, conditional=True, mimetype=mime)
    response.headers['Accept-Ranges'] = 'bytes'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route("/potplayer/<path:subpath>")
def open_potplayer(subpath):
    """用PotPlayer打开文件"""
    import subprocess
    import platform
    
    full = safe_path(app.config["ROOT_DIR"], subpath)
    if full is None or not os.path.exists(full):
        abort(404)
    
    # PotPlayer路径
    potplayer_paths = [
        "C:\\Program Files\\DAUM\\PotPlayer\\PotPlayerMini64.exe",
        "C:\\Program Files\\DAUM\\PotPlayer\\PotPlayerMini.exe",
        "C:\\Program Files (x86)\\DAUM\\PotPlayer\\PotPlayerMini.exe",
        os.path.expanduser("~\\AppData\\Local\\PotPlayer\\PotPlayerMini64.exe"),
    ]
    
    potplayer_found = None
    for path in potplayer_paths:
        if os.path.exists(path):
            potplayer_found = path
            break
    
    if potplayer_found:
        try:
            if platform.system() == "Windows":
                subprocess.Popen([potplayer_found, full], 
                                shell=False, 
                                stdout=subprocess.DEVNULL, 
                                stderr=subprocess.DEVNULL)
                return '''
                <!doctype html>
                <html>
                <head><meta charset="utf-8"><title>PotPlayer</title></head>
                <body>
                <h3>正在用 PotPlayer 打开文件...</h3>
                <p>如果 PotPlayer 没有自动启动，请手动打开。</p>
                <p><a href="/view/{}">返回播放页面</a></p>
                </body>
                </html>
                '''.format(subpath)
            else:
                return "PotPlayer 仅支持 Windows 系统"
        except Exception as e:
            return f"打开失败: {str(e)}"
    else:
        return '''
        <!doctype html>
        <html>
        <head><meta charset="utf-8"><title>PotPlayer 未找到</title></head>
        <body>
        <h3>未找到 PotPlayer</h3>
        <p>请确保已安装 PotPlayer，并且安装在默认路径。</p>
        <p><a href="/view/{}">返回播放页面</a></p>
        </body>
        </html>
        '''.format(subpath)

def select_folder_with_windows_api(title="选择文件夹"):
    """使用 PowerShell 或 tkinter 打开文件夹选择对话框"""
    import subprocess
    import sys

    # 尝试 PowerShell
    try:
        ps_script = '''
        Add-Type -AssemblyName System.Windows.Forms
        $folder = New-Object System.Windows.Forms.FolderBrowserDialog
        $folder.Description = "%s"
        $folder.ShowDialog() | Out-Null
        if ($folder.SelectedPath) {
            Write-Output $folder.SelectedPath
        }
        ''' % title
        result = subprocess.run(['powershell', '-NoProfile', '-Command', ps_script],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"PowerShell 文件夹选择出错: {e}")

    # 尝试 tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(title=title)
        root.destroy()
        if folder:
            return folder
    except Exception as e:
        print(f"tkinter 文件夹选择出错: {e}")

    return None

@app.route("/api/transcode-test")
def transcode_test():
    """诊断转码是否能正常工作"""
    import subprocess
    import tempfile
    
    try:
        # 创建一个简单的测试视频（1秒）
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp:
            tmp_path = tmp.name
        
        # 使用FFmpeg创建一个测试MP4
        ffmpeg_path = get_ffmpeg_path()
        cmd = [ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error',
               '-f', 'lavfi', '-i', 'testsrc=s=320x240:d=1',
               '-f', 'lavfi', '-i', 'sine=f=440:d=1',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '128k',
               '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+faststart',
               tmp_path]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            file_size = os.path.getsize(tmp_path)
            os.unlink(tmp_path)
            return jsonify({
                'status': 'success',
                'message': f'转码测试成功，生成了 {file_size} 字节的MP4文件',
                'file_size': file_size
            })
        else:
            error = result.stderr or result.stdout
            os.unlink(tmp_path) if os.path.exists(tmp_path) else None
            return jsonify({
                'status': 'error',
                'message': f'转码测试失败: {error[:500]}'
            }), 400
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'测试异常: {str(e)}'
        }), 400

@app.route("/api/save-reading-progress", methods=["POST"])
def api_save_reading_progress():
    """保存阅读进度"""
    data = request.get_json()
    if not data or "hash" not in data:
        return jsonify({"success": False, "error": "Missing hash"}), 400
    file_hash = data["hash"]
    progress_data = {
        "position": data.get("position"),
        "percentage": data.get("percentage")
    }
    save_reading_progress(file_hash, progress_data)
    return jsonify({"success": True})

@app.route("/api/clear-reading-progress", methods=["POST"])
def api_clear_reading_progress():
    """清除阅读进度"""
    data = request.get_json()
    if not data or "hash" not in data:
        return jsonify({"success": False, "error": "Missing hash"}), 400
    file_hash = data["hash"]
    progress_file = os.path.join(app.config['IMAGE_CACHE_DIR'], 'reading_progress.json')
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if file_hash in data:
                del data[file_hash]
                with open(progress_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                return jsonify({"success": True})
        except (json.JSONDecodeError, IOError):
            pass
    return jsonify({"success": False, "error": "Progress not found"})

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="WebCinema - 高性能媒体服务器，支持硬件加速")
    parser.add_argument("dir", nargs="?", default=".", help="要共享的目录")
    parser.add_argument("--host", default="0.0.0.0", help="绑定的主机")
    parser.add_argument("--port", type=int, default=2778, help="监听的端口")
    parser.add_argument("--workers", type=int, default=4, help="工作线程数")
    parser.add_argument("--gpu", type=int, default=0, help="GPU设备索引")
    parser.add_argument("--no-hwaccel", action="store_true", help="禁用硬件加速")
    parser.add_argument("--cache-size", type=int, default=256, help="目录缓存大小")
    
    args = parser.parse_args()

    # 如果未指定目录（使用默认值），尝试通过Windows API选择文件夹
    if args.dir == ".":
        try:
            selected = select_folder_with_windows_api("请选择要共享的目录")
            if selected:
                # 确保为绝对路径
                selected = os.path.abspath(selected)
                args.dir = selected
                print(f"已选择目录: {selected}")
            else:
                print("未选择目录，使用当前目录。")
        except Exception as e:
            print(f"文件夹选择对话框出错: {e}")
            # 继续使用默认目录

    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print("错误: 不是目录:", root)
        return
    
    app.config["ROOT_DIR"] = root
    print(f"服务根目录已设置为: {root}")
    app.config["USE_HARDWARE_ACCEL"] = not args.no_hwaccel
    app.config["GPU_DEVICE"] = args.gpu
    
    # 更新缓存大小
    global list_dir_entries_cached, get_directory_cache_key
    if args.cache_size != 256:
        list_dir_entries_cached = lru_cache(maxsize=args.cache_size)(_list_dir_entries_cached_raw)
        get_directory_cache_key = lru_cache(maxsize=args.cache_size)(_get_directory_cache_key_raw)
    
    print("=" * 60)
    print(f"WebCinema 高性能媒体服务器")
    print("=" * 60)
    print(f"服务目录: {root}")
    print(f"访问地址: http://{args.host}:{args.port}")
    print(f"硬件加速: {'已启用' if app.config['USE_HARDWARE_ACCEL'] else '已禁用'}")
    # 检测硬件加速
    hw_info = detect_hardware_acceleration()
    
    gpu_type_display = {
        'intel': 'Intel (QSV)',
        'amd': 'AMD (AMF)',
        'nvidia': 'NVIDIA (NVENC)'
    }
    
    if hw_info['gpu_type']:
        print(f"✓ 显卡类型: {gpu_type_display.get(hw_info['gpu_type'], hw_info['gpu_type'])}")
    
    if hw_info['gpu_encoders']:
        print(f"✓ 可用的 GPU 编码器: {', '.join([enc[0] for enc in hw_info['gpu_encoders']])}")
        print(f"✓ 将优先使用: {hw_info['gpu_encoders'][0][0]} 进行视频转码")
    else:
        print(f"⚠ 未检测到可用的 GPU 编码器，将使用 CPU 软件编码")
    
    if DEFFCODE_AVAILABLE:
        print(f"✓ DeFFcode: 已安装 (GPU: {args.gpu})")
    else:
        print(f"⚠ DeFFcode: 未安装，使用系统 FFmpeg 进行转码")
    print(f"目录缓存: {args.cache_size}")
    print(f"工作线程: {args.workers}")
    print("=" * 60)
    
    # 启动服务器
    app.run(
        host=args.host, 
        port=args.port, 
        threaded=True,
        debug=False
    )

if __name__ == "__main__":
    # 移除原来的 input() 调用，使用命令行参数
    main()