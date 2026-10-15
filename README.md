# OTTv2 Bot Programming Arena

## Giả định và giới hạn

- SQLite dùng khi chạy trực tiếp; Docker Compose dùng PostgreSQL. Ngày bắt đầu giải mặc định là `2026-10-15`, có thể đặt bằng `OTTV2_TOURNAMENT_START_DATE`.
- Múi giờ ngày giải là `Asia/Ho_Chi_Minh`; việc khóa/mở nhận bài và công bố do admin chủ động điều khiển, không tự khóa theo giờ.
- Không có API đăng ký công khai. Render demo tạo admin ban đầu từ `OTTV2_BOOTSTRAP_ADMIN_ID` và `OTTV2_BOOTSTRAP_ADMIN_PASSWORD`; admin có thể tạo participant qua giao diện. Cần đặt `OTTV2_SECRET_KEY` ổn định trước khi chạy nhiều lần.
- Khi triển khai có nhận bot từ người khác, đặt `OTTV2_REQUIRE_REMOTE_SANDBOX=1` và cấu hình worker Docker riêng; không chạy bot không tin cậy trực tiếp trong API.
- Admin có thể khóa nhận bài, bắt đầu chấm nền, theo dõi checkpoint và công bố leaderboard. Chấm chỉ bắt đầu khi đã khóa bài; leaderboard chỉ được ghi ra vùng public khi admin công bố.
- Scheduler APScheduler hiện cung cấp lịch callback/cleanup tùy chọn; quy trình admin thủ công là đường vận hành mặc định, không có khóa giờ 01:00/05:00/06:00.
- Rate limit login/nộp bài hiện là sliding-window trong bộ nhớ một process. Nếu chạy nhiều replica, cần chuyển bộ đếm sang Redis hoặc kho dùng chung.
- Quét AST và restricted builtins không phải ranh giới bảo mật tuyệt đối. Render demo chạy bot đã tải lên trong cùng web service, không có container isolation; chỉ mời người bạn tin tưởng. Với bot không tin cậy, dùng worker Docker cô lập như phần Sandbox bên dưới.

## Chạy cục bộ

Yêu cầu Python 3.11 trở lên.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:OTTV2_SECRET_KEY = "thay-bang-chuoi-ngau-nhien-dai-va-on-dinh"
uvicorn app:app --reload
```

Mở `http://127.0.0.1:8000`. SQLite và các thư mục storage được khởi tạo tự động. Để chạy test, cài thêm `pytest` và `httpx`, sau đó dùng `python -m pytest -q`.

### Tài khoản kiểm thử

Trên máy local dùng SQLite, chạy:

```powershell
python seed_demo_accounts.py --enable-demo-accounts
```

Lệnh tạo `admin_demo` và năm tài khoản `bot_demo01`–`bot_demo05`, mỗi lần chạy sẽ sinh mật khẩu mới. Thông tin đăng nhập được ghi vào `storage/demo-accounts.txt`, file này bị `.gitignore` loại khỏi Git. Script từ chối database PostgreSQL để tránh tạo tài khoản demo nhầm trên production.

Đăng nhập bằng `admin_demo`, sau đó dùng panel **Điều hành cuộc thi** để khóa nhận bài, chấm điểm và công bố BXH. Các tài khoản bot demo có thể nộp tệp Python qua giao diện. Mật khẩu được hash bằng Argon2id trong database.

## Docker Compose

1. Sao chép `.env.example` thành `.env`.
2. Đặt `POSTGRES_PASSWORD` mạnh; cập nhật mật khẩu trong `OTTV2_DATABASE_URL` bằng dạng URL-encoded nếu có ký tự đặc biệt.
3. Tạo secret ổn định, ví dụ `python -c "import secrets; print(secrets.token_urlsafe(48))"`, rồi đặt vào `OTTV2_SECRET_KEY`.
4. Với HTTPS phía trước Nginx, đặt `OTTV2_COOKIE_SECURE=1` và kết thúc TLS tại load balancer/reverse proxy tin cậy.
5. Chạy:

```powershell
docker compose up --build -d
```

Mở `http://localhost:8080`. Nginx phục vụ `/static/` và JSON dưới `/api/leaderboard/...` trực tiếp từ file; API không truy vấn database cho request leaderboard. Dữ liệu PostgreSQL, bài nộp, replay và leaderboard nằm trên named volumes.

## Host thử nghiệm miễn phí trên Render

[render.yaml](./render.yaml) mô tả đúng một Web Service gói Free, chạy SQLite và lưu tệp trực tiếp trên filesystem của service. Không cần PostgreSQL/S3 bên ngoài, worker từ xa hoặc tunnel. Đây chỉ là cấu hình demo: filesystem Render là ephemeral, vì vậy SQLite, tài khoản, bài bot, replay và leaderboard có thể mất khi service được thay thế hoặc khởi động lại. Không dùng cho dữ liệu quan trọng hoặc giải cần lưu trữ lâu dài.

1. Đẩy repository lên GitHub, sau đó trong Render chọn **New > Blueprint** và kết nối repository.
2. Trước khi tạo service, nhập `OTTV2_BOOTSTRAP_ADMIN_ID` (3–32 ký tự chữ/số/gạch dưới/gạch ngang) và `OTTV2_BOOTSTRAP_ADMIN_PASSWORD` (ít nhất 12 ký tự) trong các biến môi trường được yêu cầu. Dùng mật khẩu dài, ngẫu nhiên và riêng cho demo.
3. Render tự sinh `OTTV2_SECRET_KEY`. Blueprint đặt `OTTV2_DEMO_MODE=1`, SQLite cục bộ và không bật remote sandbox. Chờ deploy rồi kiểm tra `/healthz`.
4. Đăng nhập bằng tài khoản bootstrap. Dùng các trang **Bảng xếp hạng**, **Nộp bot**, **Hồ sơ & ván đấu**; trang **Quản trị** chỉ hiện trong điều hướng khi đăng nhập bằng admin. Trong trang Quản trị, nhập mã thí sinh và tự đặt mật khẩu tối thiểu 8 ký tự, sau đó gửi thông tin đăng nhập cho thí sinh qua kênh riêng. Admin hiện có không bị thay đổi mật khẩu khi service khởi động lại.

Không có self-registration và chỉ admin được mời tài khoản participant. **Cảnh báo an toàn:** bot của người tham gia chạy trong subprocess của cùng một web service; chúng không được chạy trong container riêng biệt. AST scan/restricted builtins không phải biên bảo mật đáng tin cậy. Chỉ mời người tin tưởng và không đưa dữ liệu bí mật vào demo. Với bot không tin cậy, dùng cấu hình production và worker container cô lập; các biến deploy mặc định bên ngoài Blueprint demo vẫn giữ nguyên chế độ remote opt-in như trước.

Render Free có thể ngủ và không cung cấp tính bền vững cho dữ liệu filesystem trong cấu hình này. Đây là demo tiện lợi, không phải production deployment.

## Sandbox worker

`sandbox_worker.py` nhận một JSON UTF-8 qua stdin với các trường `source`, `board` (9×9), `valid_moves` (danh sách cặp tọa độ), `player` (1 hoặc 2) và `seed` (tùy chọn). Nó chỉ ghi một JSON kết quả ra stdout. Không mount `storage/` vào container bot.

Ví dụ chạy một lượt trong Linux/Docker; payload phải được tạo từ trạng thái game hợp lệ:

```sh
cat request.json | docker run --rm -i \
  --network none --read-only --cap-drop ALL \
  --pids-limit 8 --memory 256m --cpus 1 \
  --user 10001:10001 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  ottv2:latest python sandbox_worker.py
```

`Dockerfile.bot` là image worker độc lập; `sandbox_service.py` xác thực request rồi chạy một container theo từng lượt hoặc ván đấu. API gửi source qua HTTPS tới worker, worker không nhận database URL, storage keys hay JWT secret.

## Cấu hình Nginx và tải

- `nginx.conf` bật gzip, ETag, cache control cho asset/leaderboard, giới hạn upload và proxy các route động tới FastAPI.
- Với 4.000 người xem, leaderboard được đọc từ JSON tĩnh; client phân trang 50 dòng và tìm kiếm trên dữ liệu đã tải. Nên đặt CDN/cache phía trước Nginx nếu cần phân phối nhiều vùng.
- Khối lượng dự kiến: GĐ1 khoảng 24.000 ván; GĐ2 là `1.024 / 2 × 10 × 2 = 10.240` ván; GĐ3 khoảng 100–150 ván Bo2 tùy nhánh và reset chung kết.
- Gọi `G` là tổng ván, `T` là thời gian trung bình một ván gồm khởi tạo process, `W` là số worker. Điều kiện mục tiêu 4 giờ là `G × T / W ≤ 14.400` giây. Với `G ≈ 34.400`, 8 worker cần trung bình khoảng 3,35 giây/ván hoặc nhanh hơn. Đo bằng benchmark trên máy triển khai; nếu không đạt, giảm giới hạn ply theo cấu hình run hoặc dừng sớm và xuất `partial=true` trước 05:00.
- Replay lưu tối đa 300 byte nước đi thô mỗi ván (150 ply × 2 byte); 34.400 ván tối đa xấp xỉ 10,3 MB/ngày chưa tính metadata. File zlib append-only thường nhỏ hơn; dự phòng khoảng 150 MB cho một giải 35 ngày.

## Cấu trúc và quyền riêng tư

- `storage/submissions/` và `storage/replays/` không được mount public. Submission theo `student_id`, từng phiên bản lưu SHA-256 và timestamp; endpoint tải bài chỉ dựa trên principal đã xác thực.
- Replay access phải có trong `replay_access` và người gọi phải là một bên của ván. Replay JSON không chứa source bot; danh sách replay không chứa nước đi.
- JSON leaderboard chỉ nên chứa mã công khai, rank, điểm trung bình, ngày thi đấu và trạng thái vắng; tuyệt đối không đưa hash hay source bài nộp vào đó.
- File JSON và bài nộp dùng ghi tạm rồi `os.replace`. SQLite `create_all` phù hợp chạy dev; production nên đưa schema qua migration trước khi cập nhật phiên bản.
- Docker Compose mẫu chưa cấu hình TLS, backup, Redis hoặc APScheduler tự khởi chạy. Nên vận hành bằng panel admin hoặc cấu hình callback/cleanup riêng; cần benchmark và diễn tập phục hồi trước khi mở giải thật.

## Kiểm thử

```powershell
python -m pip install pytest httpx
python -m pytest -q
```