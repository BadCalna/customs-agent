/* =============================================================================
 * web/pages/p11-conflict.js —— P11 自测页的「409 后重新拉取」接线（可 import 的模块）
 *
 * 为什么抽成模块：这段逻辑原先写在 p11.html 的内联 <script> 里，测试只能对源码做正则
 * 断言（例如「catch 里有没有 throw」）。把它变成工厂后，Node 行为测试能直接驱动
 * reload 的 resolve / reject，证明「普通重读失败不会被吞掉」——否则组件会把失败误
 * 显示成「内容已被他人修改，已刷新」。
 *
 * 这里不写业务逻辑、不发请求：reload 由页面提供（真实页面重读任务/商品/历史，
 * 自测页重读 fixture）。本模块只负责「拒绝静默失败」这一条约定。
 * ========================================================================== */

/**
 * 创建 409 后重新拉取的回调。
 *
 * @param {object} options
 * @param {Function} options.reload 重新拉取数据；抛错表示刷新未完成
 * @param {Function} [options.log] 日志回调，默认丢弃
 * @returns {(label?: string) => Promise<void>} 供组件的 onConflict 使用
 */
export function createConflictRefresh({ reload, log = () => {} } = {}) {
  if (typeof reload !== "function") {
    throw new Error("createConflictRefresh 需要 reload 回调");
  }
  return async function conflictRefresh(label = "写动作") {
    log(`${label}：收到 409，调用 onConflict 重新拉取（真实页面应重读任务/商品/历史；本页重读 fixture）。`);
    try {
      await reload();
    } catch (error) {
      const message = error && error.message ? error.message : String(error);
      log(`409 后的重新拉取失败：${message}`);
      // 记录后继续抛出：组件据此走失败分支提示「刷新未完成」，不能显示成「已刷新」。
      throw error;
    }
  };
}
