# solsrng

Discord bot (discord.py) phục vụ chơi **Sol's RNG** — theo dõi threshold, quản lý blacklist, cấu chỉnh runtime ngay qua slash command.

## Tính năng

- 📊 **Threshold tracking** — ngưỡng mặc định đặt sẵn, mọi thay đổi lưu **SQLite** (`data/`)
- ⌨️ **Slash command config** — chỉnh cấu hình runtime mà không cần sửa code
- 🔒 **Owner / co-owner** — phân quyền ghi sẵn trong `config.py`, không thể tự thêm qua command
- 🚫 **Blacklist** — danh sách người dùng hạn chế (`blacklist.txt`)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# BOT_TOKEN=...
python main.py
```

## Tài liệu chi tiết

Xem [HUONG_DAN.md](HUONG_DAN.md).
