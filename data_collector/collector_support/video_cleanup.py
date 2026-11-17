import logging

def safe_video_exit(self, exc_type, exc_val, exc_tb):
    try:
        logging.info('[视频收尾] 等待图像写盘并停止写盘线程')
        self.dataset._wait_image_writer()
        self.dataset.stop_image_writer()
        try:
            if self.dataset.episodes_since_last_encoding > 0:
                start_ep = self.dataset.num_episodes - self.dataset.episodes_since_last_encoding
                self.dataset._batch_save_episode_video(start_ep, self.dataset.num_episodes)
        finally:
            self.dataset.finalize()
        if exc_type is not None:
            logging.warning('[视频收尾] 保留未完成剧集图片用于诊断；未把失败剧集计为完成；继续抛出原始异常')
            return False
        image_root = self.dataset.root / 'images'
        if image_root.exists():
            for directory in sorted((p for p in image_root.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                image_root.rmdir()
            except OSError:
                pass
        logging.info('[视频收尾] 图像写盘与视频收尾完成')
    except Exception:
        if exc_type is None:
            raise
        logging.exception('[视频收尾] 清理异常，保留文件；原始执行错误仍会报告')
    return False

def install(recorder):
    recorder.VideoEncodingManager.__exit__ = safe_video_exit
