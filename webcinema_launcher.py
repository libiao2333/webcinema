#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebCinema 启动器 - 最终稳定版
特点：无任何交互式输入，完全自动化，专为 --noconsole 打包优化。
"""

import os
import sys
import subprocess
import threading
import time
import locale

def get_launcher_dir():
    """获取启动器（.exe或.py）所在的真实目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        return os.path.dirname(os.path.abspath(__file__))

def find_webcinema_path(launcher_dir):
    """在启动器目录下查找 webcinema.py"""
    # 1. 直接同级查找
    direct_path = os.path.join(launcher_dir, 'webcinema.py')
    if os.path.exists(direct_path):
        return direct_path
    
    # 2. 列出目录内容，精确匹配（大小写不敏感）
    for item in os.listdir(launcher_dir):
        if item.lower() == 'webcinema.py':
            return os.path.join(launcher_dir, item)
    
    # 3. 如果还没找到，尝试常见子目录（例如 ‘app’， ‘src’）
    common_subdirs = ['', 'app', 'src', 'main']
    for subdir in common_subdirs:
        check_path = os.path.join(launcher_dir, subdir, 'webcinema.py')
        if os.path.exists(check_path):
            return check_path
    
    return None

def find_python_executable():
    """查找可用的 Python 解释器"""
    candidates = ['python', 'python3', 'py']
    for cmd in candidates:
        try:
            # 使用简短超时快速检查
            result = subprocess.run([cmd, '--version'], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return cmd
        except:
            continue
    # 备用：当前解释器
    return sys.executable if sys.executable else None

def run():
    """主运行逻辑"""
    launcher_dir = get_launcher_dir()
    print(f"启动器目录: {launcher_dir}")
    
    # 1. 查找主程序
    webcinema_path = find_webcinema_path(launcher_dir)
    if not webcinema_path:
        print(f"❌ 错误：在目录下未找到 'webcinema.py'")
        print(f"   目录内容: {os.listdir(launcher_dir)}")
        print("程序将在 5 秒后自动退出...")
        time.sleep(5)
        sys.exit(1)
    
    print(f"✅ 找到主程序: {webcinema_path}")
    webcinema_dir = os.path.dirname(webcinema_path)
    
    # 2. 查找 Python
    python_cmd = find_python_executable()
    if not python_cmd:
        print("❌ 错误：未找到 Python 解释器。请确保已安装 Python 3.10+ 并已添加到 PATH")
        print("程序将在 5 秒后自动退出...")
        time.sleep(5)
        sys.exit(1)
    print(f"✅ 使用 Python: {python_cmd}")
    
    # 3. 显示启动信息
    print("\n" + "="*50)
    print("     WebCinema 影音库服务器")
    print("="*50)
    print("启动成功！")
    print(f"• 本地访问: http://127.0.0.1:5000")
    print(f"• 网络访问: http://<本机IP>:5000")
    print("\n提示：关闭此窗口即可停止服务器")
    print("="*50 + "\n")
    
    # 4. 启动 Flask 子进程
    process = None
    try:
        process = subprocess.Popen(
            [python_cmd, webcinema_path],
            cwd=webcinema_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=locale.getpreferredencoding(),
            errors='replace',
            bufsize=1
        )
        
        # 输出重定向线程
        def output_reader(proc):
            for line in iter(proc.stdout.readline, ''):
                if line.strip():
                    # 可在此处过滤或格式化 Flask 输出
                    print(f"> {line.rstrip()}")
        
        reader_thread = threading.Thread(target=output_reader, args=(process,))
        reader_thread.daemon = True
        reader_thread.start()
        
        # 等待进程结束
        process.wait()
        
        if process.returncode != 0:
            print(f"\n⚠ 服务器进程异常退出，代码: {process.returncode}")
        else:
            print(f"\n✅ 服务器已正常停止")
            
    except KeyboardInterrupt:
        print("\n\n🛑 正在停止服务器...")
        if process:
            process.terminate()
            time.sleep(1)
            if process.poll() is None:
                process.kill()
        print("已停止")
    except Exception as e:
        print(f"\n❌ 启动失败: {e}")
    
    # 5. 退出前暂停（仅在有控制台时）
    print("\n" + "="*50)
    print("启动器运行结束")
    
    # 检查是否有控制台，有则等待，无则直接退出
    try:
        sys.stdin.fileno()
        # 有控制台，等待用户查看
        input("按 Enter 键退出...")
    except:
        # 无控制台，自动延迟后退出
        time.sleep(3)
    
    sys.exit(0 if (process and process.returncode == 0) else 1)

if __name__ == "__main__":
    run()