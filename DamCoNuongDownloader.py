import os
import time
import re
import threading
import requests
from bs4 import BeautifulSoup
import img2pdf
import tkinter as tk
from tkinter import messagebox, ttk

class ComicDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Dâm Cô Nương PDF Downloader")
        self.root.geometry("620x500")
        self.root.resizable(False, False)

        # Title
        title_label = tk.Label(root, text="Ứng Dụng Tải Truyện Dâm Cô Nương sang PDF", font=("Arial", 14, "bold"), fg="#d32f2f")
        title_label.pack(pady=15)

        # Form Frame
        form_frame = tk.Frame(root)
        form_frame.pack(pady=5, padx=20, fill="x")

        # URL
        tk.Label(form_frame, text="Link Chương/Truyện:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w", pady=6)
        self.url_entry = tk.Entry(form_frame, width=48, font=("Arial", 10))
        self.url_entry.grid(row=0, column=1, pady=6, padx=5)

        # Category
        tk.Label(form_frame, text="Thể loại (Thư mục):", font=("Arial", 10)).grid(row=1, column=0, sticky="w", pady=6)
        self.cat_entry = tk.Entry(form_frame, width=48, font=("Arial", 10))
        self.cat_entry.insert(0, "Manhwa")
        self.cat_entry.grid(row=1, column=1, pady=6, padx=5)

        # Comic Title
        tk.Label(form_frame, text="Tên Truyện:", font=("Arial", 10)).grid(row=2, column=0, sticky="w", pady=6)
        self.comic_entry = tk.Entry(form_frame, width=48, font=("Arial", 10))
        self.comic_entry.insert(0, "Ten_Truyen")
        self.comic_entry.grid(row=2, column=1, pady=6, padx=5)

        # Chapter Title
        tk.Label(form_frame, text="Tên Chương:", font=("Arial", 10)).grid(row=3, column=0, sticky="w", pady=6)
        self.chap_entry = tk.Entry(form_frame, width=48, font=("Arial", 10))
        self.chap_entry.insert(0, "Chapter_01")
        self.chap_entry.grid(row=3, column=1, pady=6, padx=5)

        # Button
        self.download_btn = tk.Button(root, text="Bắt Đầu Tải PDF & Phân Mục", font=("Arial", 11, "bold"), bg="#1976D2", fg="white", padx=10, pady=5, command=self.start_download_thread)
        self.download_btn.pack(pady=15)

        # Log Box
        tk.Label(root, text="Nhật ký hoạt động:", font=("Arial", 10, "bold")).pack(anchor="w", padx=20)
        self.log_box = tk.Text(root, height=10, width=72, font=("Consolas", 9))
        self.log_box.pack(pady=5, padx=20)

    def log(self, message):
        self.log_box.insert(tk.END, message + "\n")
        self.log_box.see(tk.END)

    def start_download_thread(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Lỗi", "Vui lòng nhập URL chương truyện!")
            return
        
        self.download_btn.config(state=tk.DISABLED)
        threading.Thread(target=self.download_process, daemon=True).start()

    def download_process(self):
        url = self.url_entry.get().strip()
        category = re.sub(r'[/\\*?:"<>|]', "", self.cat_entry.get().strip()) or "Manhwa"
        comic_title = re.sub(r'[/\\*?:"<>|]', "", self.comic_entry.get().strip()) or "Truyen"
        chapter_title = re.sub(r'[/\\*?:"<>|]', "", self.chap_entry.get().strip()) or "Chapter"

        output_dir = os.path.join("Truyen_Da_Tai", category, comic_title)
        output_pdf = os.path.join(output_dir, f"{chapter_title}.pdf")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://damconuong.cool/'
        }

        self.log(f"Đang kết nối tới trang...")
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                self.log(f"[Lỗi] Không thể truy cập trang (Mã lỗi: {res.status_code})")
                self.download_btn.config(state=tk.NORMAL)
                return
        except Exception as e:
            self.log(f"[Lỗi kết nối] {e}")
            self.download_btn.config(state=tk.NORMAL)
            return

        soup = BeautifulSoup(res.text, 'html.parser')
        img_tags = soup.select('.reading-detail img, .chapter-content img, .page-chapter img, .viewer-content img')

        if not img_tags:
            self.log("[Lỗi] Không tìm thấy ảnh trong chapter này!")
            self.download_btn.config(state=tk.NORMAL)
            return

        temp_dir = 'temp_imgs_app'
        os.makedirs(temp_dir, exist_ok=True)
        image_paths = []

        self.log(f"Tìm thấy {len(img_tags)} trang ảnh. Đang tải xuống...")
        for idx, img in enumerate(img_tags):
            img_url = img.get('data-src') or img.get('src') or img.get('data-original')
            if img_url:
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                try:
                    img_data = requests.get(img_url, headers=headers, timeout=10).content
                    img_path = os.path.join(temp_dir, f'img_{idx:04d}.jpg')
                    with open(img_path, 'wb') as f:
                        f.write(img_data)
                    image_paths.append(img_path)
                except Exception as e:
                    self.log(f"Không tải được ảnh {idx}: {e}")

        if image_paths:
            os.makedirs(output_dir, exist_ok=True)
            try:
                self.log("Đang tạo file PDF...")
                with open(output_pdf, 'wb') as f:
                    f.write(img2pdf.convert(image_paths))
                self.log(f"[Thành công] Đã lưu tại: {output_pdf}")
                messagebox.showinfo("Hoàn tất", f"Đã tải xong file PDF tại:\n{output_pdf}")
            except Exception as e:
                self.log(f"[Lỗi tạo PDF] {e}")

            # Dọn dẹp
            for p in image_paths:
                if os.path.exists(p):
                    os.remove(p)
            try:
                os.rmdir(temp_dir)
            except:
                pass
        
        self.download_btn.config(state=tk.NORMAL)

if __name__ == "__main__":
    root = tk.Tk()
    app = ComicDownloaderApp(root)
    root.mainloop()
