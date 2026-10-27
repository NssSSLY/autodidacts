"""浏览器资料/配置的跨进程非阻塞锁；退出自动释放，不删除资料或窃取浏览器会话。"""

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lease(path):
    """使用操作系统文件锁排斥同一资料并发操作，进程异常退出也会释放。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError(
                    "资料正在被另一个操作使用，请先关闭登录窗口或等待操作结束"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("资料正在被另一个操作使用，请稍后再试") from exc
        yield
    finally:
        os.close(fd)


def profile_lease(profile):
    """以规范化资料路径生成旁路锁，同一资料的检查、登录和提问不能重叠。"""
    return file_lease(str(Path(profile).resolve()) + ".autodidact.lock")
