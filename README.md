# OTTv2 Bot Programming Arena

## Giả định và giới hạn

- SQLite dùng khi chạy trực tiếp; Docker Compose dùng PostgreSQL. Ngày bắt đầu giải mặc định là `2026-10-15`, có thể đặt bằng `OTTV2_TOURNAMENT_START_DATE`.
- Múi giờ ngày giải là `Asia/Ho_Chi_Minh`; việc khóa/mở nhận bài và công bố do admin chủ động điều khiển, không tự khóa theo giờ.
- Tài khoản được tạo qua công cụ vận hành, không có API đăng ký công khai. Cần đặt `OTTV2_SECRET_KEY` ổn định trước khi chạy nhiều lần.
- Khi triển khai có nhận bot từ người khác, đặt `OTTV2_REQUIRE_REMOTE_SANDBOX=1` và cấu hình worker Docker riêng; không chạy bot không tin cậy trực tiếp trong API.
- Admin có thể khóa nhận bài, bắt đầu chấm nền, theo dõi checkpoint và công bố leaderboard. Chấm chỉ bắt đầu khi đã khóa bài; leaderboard chỉ được ghi ra vùng public khi admin công bố.
- Scheduler APScheduler hiện cung cấp lịch callback/cleanup tùy chọn; quy trình admin thủ công là đường vận hành mặc định, không có khóa giờ 01:00/05:00/06:00.
- Rate limit login/nộp bài hiện là sliding-window trong bộ nhớ một process. Nếu chạy nhiều replica, cần chuyển bộ đếm sang Redis hoặc kho dùng chung.
- Quét AST và restricted builtins không phải ranh giới bảo mật tuyệt đối. Dùng container worker cô lập như phần Sandbox bên dưới; không chạy bot không tin cậy trên máy có dữ liệu bí mật.

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

Repo có [render.yaml](./render.yaml) để tạo Web Service gói Free. Trước khi mở dịch vụ, cần có PostgreSQL bên ngoài, bucket S3-compatible cho file và worker Docker riêng. Ví dụ có thể dùng PostgreSQL managed và Cloudflare R2 free tier; hãy kiểm tra giới hạn, chính sách ngừng dịch vụ/xóa dữ liệu và sao lưu của nhà cung cấp trước khi dùng. Không dùng filesystem của Render Web Service để giữ database, bot, replay hoặc leaderboard.

### 1. Chuẩn bị lưu trữ

- Tạo PostgreSQL managed bên ngoài Render, lấy connection URL. Đặt dialect SQLAlchemy thành `postgresql+psycopg://` (Render PostgreSQL Free có thể hết hạn sau 30 ngày).
- Tạo bucket riêng tư trong dịch vụ S3-compatible và access key chỉ có quyền đọc/ghi bucket đó. Cần các giá trị `OTTV2_S3_ENDPOINT`, `OTTV2_S3_BUCKET`, `OTTV2_S3_ACCESS_KEY_ID`, `OTTV2_S3_SECRET_ACCESS_KEY`; `OTTV2_S3_REGION` mặc định là `auto`.
- URL database, S3 keys và worker token là secrets. Không commit chúng vào Git hoặc gửi chúng qua chat.

### 2. Chuẩn bị máy Windows làm worker cô lập

Máy này phải bật, có Internet và Docker Desktop đang chạy trong lúc người dùng nộp bot hoặc admin chấm giải. Mỗi lượt chạy được giao cho container riêng, không mạng, filesystem chỉ đọc, không capabilities, giới hạn 256 MiB RAM và một CPU.

Trong PowerShell tại thư mục dự án:

```powershell
docker build -f Dockerfile.bot -t ottv2-bot-worker:latest .
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Giữ bí mật token vừa tạo. Đặt cùng giá trị vào biến môi trường của worker và Render. Chạy API worker ở terminal riêng:

```powershell
$env:OTTV2_SANDBOX_TOKEN = "đặt-token-đã-tạo-ở-trên"
python -m uvicorn sandbox_service:app --host 127.0.0.1 --port 9000
```

Cài `cloudflared`, rồi trong terminal khác tạo HTTPS tunnel:

```powershell
cloudflared tunnel --url http://127.0.0.1:9000
```

Lệnh sẽ in URL HTTPS công khai. Với Quick Tunnel, URL có thể đổi khi tạo tunnel lại; khi đổi, cập nhật `OTTV2_SANDBOX_URL` trên Render. Worker chỉ chấp nhận request có bearer token. Không mở port worker trực tiếp trên router/firewall; dùng HTTPS tunnel.

### 3. Deploy Web Service

1. Đẩy repository lên GitHub, sau đó trong Render chọn **New > Blueprint** và kết nối repository.
2. Render đọc `render.yaml`. Trong Environment của service, điền các giá trị `OTTV2_DATABASE_URL`, `OTTV2_SANDBOX_URL`, `OTTV2_SANDBOX_TOKEN` và bốn biến `OTTV2_S3_*` bắt buộc.
3. Chờ deploy và kiểm tra `/healthz`. Free Web Service có thể ngủ sau thời gian không có truy cập; lần mở lại thường chậm. Dữ liệu chính vẫn nằm ở PostgreSQL/S3 bên ngoài.

`OTTV2_REQUIRE_REMOTE_SANDBOX=1` khiến app không khởi động nếu thiếu worker HTTPS và token; không tự rơi về chạy bot trong tiến trình API. Worker offline sẽ làm upload/chấm báo lỗi cho tới khi máy Windows và tunnel hoạt động trở lại.

### 4. Cấp tài khoản mời

Không có đăng ký công khai. Tạo admin đầu tiên và từng thí sinh bằng lệnh vận hành sau khi đặt `OTTV2_DATABASE_URL` vào môi trường PowerShell:

```powershell
python create_account.py --student-id admin_doanh --admin
python create_account.py --student-id bot_01
```

Script hỏi mật khẩu hai lần, không nhận mật khẩu qua tham số dòng lệnh. Chỉ cấp `--admin` cho người tin cậy. Gửi mã tài khoản và mật khẩu cho người được mời qua kênh riêng.

Free hosting phù hợp để thử, không có cam kết uptime hoặc backup. Render Free vẫn có thể ngủ; nhà cung cấp PostgreSQL/S3 bên ngoài cũng có giới hạn riêng. Sao lưu dữ liệu định kỳ và không dùng cho giải có dữ liệu quan trọng nếu chưa diễn tập khôi phục.

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