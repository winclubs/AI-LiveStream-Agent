// 兼容性别名脚本：确保历史缓存引用 15_gpu_avatar.js 时安全加载 15_settings_gpu.js
if (typeof loadGpuAvatarProviders === 'undefined') {
    const s = document.createElement('script');
    s.src = '/static/js/modules/15_settings_gpu.js?v=2.0.7';
    document.head.appendChild(s);
}
