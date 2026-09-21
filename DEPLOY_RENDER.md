# Đưa hệ thống V2 lên GitHub + Render

Bản này deploy **frontend React + backend FastAPI chung một Web Service**. FastAPI phục vụ file React đã build nên website và API dùng cùng một domain; cookie đăng nhập hoạt động đơn giản hơn và không cần một Static Site riêng.

## Biến môi trường production

- `DATABASE_URL`: lấy từ Render Postgres.
- `SECRET_KEY`: chuỗi ngẫu nhiên >= 32 ký tự.
- `ADMIN_PASSWORD`: mật khẩu admin khởi tạo lần đầu, >= 10 ký tự.
- `GEMINI_API_KEY`: Gemini API key.
- `GEMINI_MODEL`: model tài khoản hỗ trợ, mặc định `gemini-3.8-flash`.
- `COOKIE_SECURE=true`.

`python -m app.init_db` chạy mỗi lần container khởi động nhưng không reset dữ liệu. Khi database đã có tài khoản, lệnh thoát mà không sửa tài khoản hiện có.

## Cách dễ nhất: Render Blueprint

1. Push toàn bộ thư mục này lên một GitHub repository.
2. Render Dashboard -> New -> Blueprint.
3. Chọn repository, Render đọc `render.yaml`.
4. Khi được hỏi secret, nhập `ADMIN_PASSWORD` và `GEMINI_API_KEY`.
5. Apply Blueprint.
6. Đợi Postgres và Web Service deploy xong.
7. Mở URL `https://<service>.onrender.com` và đăng nhập `admin` bằng `ADMIN_PASSWORD`.

## Cách thủ công

1. Render -> New -> Postgres -> chọn cùng region với web service.
2. Render -> New -> Web Service -> chọn GitHub repository.
3. Language = Docker, branch = main. Dockerfile nằm ở root.
4. Thêm các biến môi trường ở trên. `DATABASE_URL` dùng Internal Database URL của Render Postgres.
5. Health Check Path = `/api/health`.
6. Deploy. Docker CMD tự build frontend, khởi tạo schema/admin lần đầu và chạy Uvicorn trên `$PORT`.
