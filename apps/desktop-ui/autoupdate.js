/**
 * 自动更新模块 (electron-updater 封装)
 *
 * 设计原则：**更新失败绝不能阻断正常开播**。
 * 数字人直播节点是长时间运行的生产设备，更新链路异常时必须静默降级，
 * 而不是把控制台变成启动即崩。因此：
 *   - 所有失败路径都只记录日志，不抛异常；
 *   - 开发态 (app.isPackaged === false) 完全不启用；
 *   - 未配置更新服务器时静默关闭。
 *
 * 刻意只依赖 electron-updater 与 Electron 内置模块，
 * 不引入 electron-log —— 减少一个原生依赖即减少一处打包失败点。
 */
const { app, dialog } = require("electron");

const UPDATE_SERVER_URL = process.env.UPDATE_SERVER_URL || "";

let autoUpdater = null;
try {
  ({ autoUpdater } = require("electron-updater"));
} catch (error) {
  autoUpdater = null;
}

function log(level, ...args) {
  const prefix = "[auto-updater]";
  const line = [prefix, ...args].join(" ");
  if (level === "error") console.error(line);
  else if (level === "warn") console.warn(line);
  else console.log(line);
}

function isEnabled() {
  return app.isPackaged && Boolean(UPDATE_SERVER_URL) && Boolean(autoUpdater);
}

function initAutoUpdater() {
  if (!isEnabled()) {
    log("info", "未启用自动更新 (开发态、未配置 UPDATE_SERVER_URL 或依赖缺失)");
    return false;
  }
  try {
    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = true;
    autoUpdater.on("update-available", (info) => log("info", `新版本可下载: ${info?.version}`));
    autoUpdater.on("update-not-available", () => log("info", "已是最新版本"));
    autoUpdater.on("error", (err) => log("warn", `更新链路异常 (已忽略，不影响开播): ${err?.message || err}`));
    autoUpdater.on("update-downloaded", (info) => log("info", `新版本已下载，重启后自动安装: ${info?.version}`));
    log("info", `自动更新已启用: ${UPDATE_SERVER_URL}`);
    return true;
  } catch (error) {
    log("warn", `自动更新初始化失败 (已忽略): ${error?.message || error}`);
    return false;
  }
}

async function checkForUpdates() {
  if (!isEnabled()) return { checked: false, reason: "disabled" };
  try {
    const result = await autoUpdater.checkForUpdates();
    const version = result?.updateInfo?.version;
    return { checked: true, version: version || null };
  } catch (error) {
    log("warn", `检查更新失败 (已忽略，不影响开播): ${error?.message || error}`);
    return { checked: false, reason: "error", error: String(error?.message || error) };
  }
}

/** 手动检查更新（渲染层按钮调用）。仅在用户显式点击时弹窗反馈。 */
async function checkForUpdatesInteractive(parentWindow) {
  if (!isEnabled()) {
    if (parentWindow) {
      await dialog.showMessageBox(parentWindow, {
        type: "info",
        title: "自动更新未启用",
        message: "当前构建未启用自动更新。",
        detail: "请设置 UPDATE_SERVER_URL 环境变量并重新构建安装包。",
        buttons: ["知道了"],
      });
    }
    return { checked: false, reason: "disabled" };
  }

  const result = await checkForUpdates();
  if (result.checked && result.version && parentWindow) {
    const { response } = await dialog.showMessageBox(parentWindow, {
      type: "info",
      title: "发现新版本",
      message: `发现新版本 v${result.version}`,
      detail: "将在下载完成后，于下次重启应用时自动安装。",
      buttons: ["稍后重启", "立即重启"],
      defaultId: 1,
      cancelId: 0,
    });
    if (response === 1) {
      try {
        autoUpdater.quitAndInstall(false, true);
      } catch (error) {
        log("warn", `触发安装失败 (已忽略): ${error?.message || error}`);
      }
    }
  } else if (!result.checked && parentWindow && result.reason === "error") {
    await dialog.showMessageBox(parentWindow, {
      type: "warning",
      title: "检查更新失败",
      message: "无法连接更新服务器。",
      detail: "当前版本仍可正常使用，请稍后重试或手动下载安装包。",
      buttons: ["知道了"],
    });
  }
  return result;
}

module.exports = {
  initAutoUpdater,
  checkForUpdates,
  checkForUpdatesInteractive,
  isEnabled,
};