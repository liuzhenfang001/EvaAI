import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "EvaAI",
  description: "企业级个人 AI 助手",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
