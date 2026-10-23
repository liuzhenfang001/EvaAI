import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

/**
 * vitest 只认自己的配置，不读 `tsconfig.json` 的 `paths`——所以 `@/` 这个别名要在这里
 * 再说一遍，否则**任何 import 了 `@/...` 的模块都没法单测**（报错是
 * `Cannot find package '@/core/api/config'`）。
 *
 * 这就是它存在的全部理由：`src/` 下的源码统一用 `@/` 前缀互相引用，而单测是这个项目里
 * 唯一一处自己解析模块的地方（Next 那边由构建器处理）。
 *
 * 与 `tsconfig.json` 的 `paths`（`"@/*": ["./src/*"]`）成对，改一处就要改另一处。
 */
export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
