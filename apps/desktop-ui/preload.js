/**
 * 预加载脚本：在 contextIsolation + sandbox 下暴露最小受控 IPC 桥。
 *
 * 只暴露两个只读/幂等能力，绝不暴露 ipcRenderer 本身、
 * 绝不暴露 fs / child_process / 任意 invoke 通道。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopBridge", {
  /** 触发自动更新检查（失败由主进程静默处理，绝不抛出到渲染层） */
  checkForUpdates: () => ipcRenderer.invoke("app:check-for-updates"),
  /** 读取桌面壳与运行时版本信息 */
  getVersion: () => ipcRenderer.invoke("app:get-version"),
  /** 标记为 Electron 桌面环境，供前端区分浏览器访问 */
  isDesktop: true,
});