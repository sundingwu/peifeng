import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./styles/app.css";

/** 字体与基础密度：与 `styles/app.css` 里的 `--sans` 保持一致，让 antd 控件融入同一套排版。 */
const SANS =
  '"Inter", "Segoe UI Variable Text", -apple-system, "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 状态由 SSE 增量驱动，查询只负责"拉快照对账"，因此不做轮询
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 5_000,
    },
  },
});

/**
 * antd 主题：只覆盖"我们真的在意"的几类 token——圆角、控件高度、灰阶与强调色。
 * 这些值要和 app.css 的 CSS 变量对齐，否则自绘部分与 antd 控件会看起来像两套设计。
 */
function designTokens(dark: boolean) {
  return {
    token: {
      fontFamily: SANS,
      fontSize: 13,
      borderRadius: 8,
      borderRadiusLG: 12,
      borderRadiusSM: 6,
      controlHeight: 30,
      controlHeightSM: 26,
      controlHeightLG: 36,
      colorPrimary: dark ? "#8ac7a5" : "#65745d",
      colorInfo: dark ? "#8ac7a5" : "#65745d",
      colorSuccess: dark ? "#37c98a" : "#64775c",
      colorWarning: dark ? "#e0a350" : "#ad844f",
      colorError: dark ? "#f0736c" : "#a45e4e",
      colorBgLayout: dark ? "#101713" : "#f6f1e7",
      colorBgContainer: dark ? "#18221c" : "#fcf9f1",
      colorBgElevated: dark ? "#1d2922" : "#fcf9f1",
      colorBorder: dark ? "#2b3930" : "#ded5c4",
      colorBorderSecondary: dark ? "#1c1f26" : "#ece5d8",
      colorText: dark ? "#e6eee7" : "#3f463a",
      colorTextSecondary: dark ? "#a8b9ab" : "#747b6b",
      colorTextTertiary: dark ? "#809385" : "#959885",
      colorFillTertiary: dark ? "rgba(255,255,255,.06)" : "rgba(17,17,26,.045)",
      colorFillQuaternary: dark ? "rgba(255,255,255,.04)" : "rgba(17,17,26,.03)",
      motionDurationMid: "0.14s",
      wireframe: false,
      boxShadowSecondary: dark
        ? "0 2px 8px rgba(0,0,0,.45), 0 28px 64px -28px rgba(0,0,0,.75)"
        : "0 2px 6px rgba(16,18,24,.06), 0 24px 60px -24px rgba(16,18,24,.24)",
    },
    components: {
      Button: { paddingInline: 12, fontWeight: 500, defaultShadow: "none", primaryShadow: "none" },
      Input: { paddingBlock: 5 },
      Modal: { titleFontSize: 15, headerBg: "transparent" },
      Dropdown: { paddingBlock: 5 },
      Tooltip: { fontSize: 12 },
    },
  };
}

function Root() {
  const { token, components } = designTokens(false);
  return (
    <ConfigProvider locale={zhCN} theme={{ algorithm: theme.defaultAlgorithm, token, components }}>
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <App />
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  );
}

const container = document.getElementById("root");
if (!container) throw new Error("缺少 #root 容器");
createRoot(container).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
