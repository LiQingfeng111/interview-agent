# 前端镜像：Vite 构建 → Nginx 静态托管 + API 代理

FROM node:22-alpine AS build

WORKDIR /app

# 先装依赖（利用缓存层）
COPY package.json package-lock.json* ./
RUN npm ci || npm install

# 再拷源码并构建
COPY . .
RUN npm run build

# ---------------------------------------------------------------- 运行阶段
FROM nginx:1.27-alpine

# 自定义配置：SPA history 模式 + /api 反向代理
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q --spider http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
