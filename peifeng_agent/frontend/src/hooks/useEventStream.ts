/**
 * SSE 事件流：把内核事件按 seq 增量灌进 React。
 *
 * 用 `@microsoft/fetch-event-source` 而不是浏览器原生 EventSource，原因是它支持在
 * 断线后带上下文重连、并且能读到 HTTP 状态码（原生 EventSource 遇到非 200 会直接停止重连）。
 *
 * **多会话并发**：连接时带上 `session`，服务端只推这个会话的事件（否则两个任务的
 * 事件会搅在一起）；切换会话时通过依赖变化重连。按会话过滤后 seq 天然不连续，
 * 所以不再用"跳号"判断丢失——改为每次重连成功时对账一次快照。
 */
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { useEffect, useRef, useState } from "react";

import type { AgentEvent } from "../api/client";
import { endpoints } from "../api/client";

export type StreamStatus = "connecting" | "open" | "closed";

export function useEventStream(
  initialSeq: number,
  onEvent: (event: AgentEvent) => void,
  onDesync: () => void,
  session = "",
): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const lastSeq = useRef(initialSeq);
  const handlers = useRef({ onEvent, onDesync });
  handlers.current = { onEvent, onDesync };

  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    let connected = false;

    const connect = async () => {
      try {
        const query = new URLSearchParams({ after: String(lastSeq.current) });
        if (session) query.set("session", session);
        await fetchEventSource(`${endpoints.events}?${query.toString()}`, {
          signal: controller.signal,
          openWhenHidden: true,
          onopen: async (response) => {
            if (!response.ok) throw new Error(`事件流响应异常：${response.status}`);
            setStatus("open");
            // 重连成功后对账一次：断线期间的事件可能已经不在服务端缓冲里了
            if (connected) handlers.current.onDesync();
            connected = true;
          },
          onmessage: (message) => {
            if (!message.data) return;
            let event: AgentEvent;
            try {
              event = JSON.parse(message.data) as AgentEvent;
            } catch {
              return; // 忽略无法解析的帧
            }
            if (typeof event.seq === "number") {
              lastSeq.current = Math.max(lastSeq.current, event.seq);
            }
            handlers.current.onEvent(event);
          },
          onerror: () => {
            if (stopped) return;
            setStatus("connecting");
            return 1000; // 返回重试间隔；抛出异常会终止重连
          },
          onclose: () => {
            if (!stopped) throw new Error("事件流被服务端关闭");
          },
        });
      } catch {
        if (!stopped) setStatus("closed");
      }
    };

    void connect();
    return () => {
      stopped = true;
      controller.abort();
      setStatus("closed");
    };
  }, [session]);

  return status;
}
