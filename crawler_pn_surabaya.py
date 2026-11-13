import argparse, json, os, random, re, time
from collections import Counter

from scrapling.fetchers import StealthySession

BASE = "https://putusan3.mahkamahagung.go.id"
URL_KATEGORI = (
    BASE
    + "/direktori/index/pengadilan/pn-surabaya/kategori/"
    "narkotika-dan-psikotropika-1/tahunjenis/putus/tahun/{tahun}.html"
)
RE_DETAIL = re.compile(r"/direktori/putusan/[A-Za-z0-9]+\.html")
# nomor perkara PN Surabaya, mis. 123/Pid.Sus/2024/PN Sby  (kode pengadilan Surabaya = "Sby")
RE_NOMOR = re.compile(r"\d+/Pid\.[A-Za-z.]+/\d{4}/PN\s*(?:Sby|Surabaya)", re.I)
DELAY = (8.0, 15.0)
TANTANGAN = ("just a moment", "checking your browser", "verify you are human", "memverifikasi", "enable javascript and cookies")
BLOKIR = ("error 1015", "you are being rate limited", "error 1020", "sorry, you have been blocked", "access denied")
# alasan yang layak dicoba ulang pada sesi berikutnya
ULANG = ("halaman tidak termuat", "gagal unduh PDF")   # "tautan PDF tidak ada" dianggap permanen (pakai --ulang-tanpa-pdf untuk mencoba lagi)


def jeda():
    time.sleep(random.uniform(*DELAY))


def teks_body(response):
    try:
        return str(response.css("body").first.get_all_text()).lower()
    except (AttributeError, TypeError):
        return ""


def terblokir(response):
    t = teks_body(response)
    return any(x in t for x in BLOKIR)


def siap(response, selector, timeout=20000):
    return not selector or len(response.css(selector)) > 0


def semua_anchor(response):
    return [{"href": response.urljoin(a.attrib.get("href", "")),
             "text": str(a.get_all_text(strip=True))}
            for a in response.css("a[href]")]


def semua_href(response):
    return [a["href"] for a in semua_anchor(response)]


def basis_url(u):
    return re.sub(r"/page/\d+\.html$", "", u).removesuffix(".html")


def nomor_halaman(u):
    m = re.search(r"/page/(\d+)\.html$", u)
    return int(m.group(1)) if m else 1


def buka(session, url, referer=None, timeout=60000, page_action=None):
    """Fetch yang tahan terhadap timeout / redirect (tidak membuat program crash)."""
    for i in range(3):
        try:
            headers = {"Referer": referer} if referer and i == 0 else {}
            return session.fetch(url, timeout=timeout, wait=500, page_action=page_action,
                                 extra_headers=headers)
        except Exception as e:
            print("   [navigasi gagal]", str(e).splitlines()[0][:110])
            time.sleep(5 * (i + 1))
    return None


def tanya_berhenti():
    print("\n!! Beberapa halaman berturut-turut gagal dimuat (mungkin Cloudflare / pembatasan server).")
    j = input("   Periksa jendela browser & selesaikan verifikasi bila ada. ENTER = lanjut, 'q' = berhenti: ")
    return j.strip().lower() == "q"


def url_halaman(basis, n):
    return basis + ".html" if n == 1 else f"{basis}/page/{n}.html"


def link_dari_halaman(response):
    """Ambil link putusan dari daftar utama saja (abaikan sidebar/putusan terkait milik pengadilan lain)."""
    detail = [a for a in semua_anchor(response) if RE_DETAIL.search(a["href"])]
    utama = [a for a in detail if RE_NOMOR.search(a["text"])]
    pakai = utama if utama else detail
    hasil = []
    for a in pakai:
        h = a["href"].split("#")[0]
        if h not in hasil:
            hasil.append(h)
    return hasil, len(detail)


def ambil_halaman_daftar(session, u, n):
    """Kembalikan list link; [] hanya jika pengguna mengonfirmasi bahwa memang akhir daftar; None jika terblokir."""
    while True:
        for percobaan in range(3):
            if percobaan == 0:
                if percobaan == 0:
                    jeda()
                response = buka(session, u)
            else:
                print(f"  halaman {n}: kosong, muat ulang (percobaan {percobaan + 1}/3)...")
                time.sleep(20 * percobaan)
                response = buka(session, u)
            if response and siap(response, "a[href*='/direktori/putusan/']", timeout=30000):
                return link_dari_halaman(response)
            if response and terblokir(response):
                print("  !! Terdeteksi pembatasan server. Berhenti.")
                return None
        print(f"  halaman {n}: tetap kosong setelah 3 percobaan.")
        j = input("   Lihat jendela browser. Jika daftar masih berisi -> ENTER (coba lagi); "
                  "jika memang akhir daftar -> ketik 'selesai': ")
        if j.strip().lower() == "selesai":
            return [], 0


def kumpulkan_link(session, url_awal, maks_halaman=15, maks_link=150):
    basis = basis_url(url_awal)
    hasil = []
    for n in range(1, maks_halaman + 1):
        if len(hasil) >= maks_link:
            break
        r = ambil_halaman_daftar(session, url_halaman(basis, n), n)
        if r is None:
            break
        links, total_anchor = r
        if not links:
            print(f"  halaman {n}: akhir daftar.")
            break
        baru = [l for l in links if l not in hasil]
        hasil.extend(baru)
        print(f"  halaman {n}: +{len(baru)} link (total {len(hasil)}; {total_anchor} anchor putusan di halaman)")
        if not baru:        # halaman berulang -> sudah lewat akhir daftar
            break
    return hasil


def baca_meta(response):
    meta = {}
    for tr in response.css("tr"):
        sel = [str(c.get_all_text(strip=True)) for c in tr.css("td, th")]
        if len(sel) >= 2 and sel[0]:
            meta[sel[0].strip(" :").lower()] = sel[1]
    return meta


def berkekuatan_tetap(meta):
    for k, v in meta.items():
        if any(x in k for x in ("status", "tingkat", "proses", "amar")):
            if re.search(r"berkekuatan\s+hukum\s+tetap|inkracht", v, re.I):
                return True
    return False


def alasan_tolak(meta):
    if not meta or "nomor" not in meta:
        return "halaman tidak termuat"
    lembaga = meta.get("lembaga peradilan", "")
    if lembaga and "surabaya" not in lembaga.lower():
        return f"bukan PN Surabaya ({lembaga})"
    klas = meta.get("klasifikasi", "")
    if klas and "narkotika" not in klas.lower():
        return f"bukan narkotika ({klas[:40]})"
    tingkat = meta.get("tingkat proses", "")
    if tingkat and "pertama" not in tingkat.lower():
        return f"bukan tingkat pertama ({tingkat})"
    if berkekuatan_tetap(meta):
        return "berkekuatan hukum tetap"
    return None


def cari_link_pdf(anchors):
    """Pilih tautan PDF terbaik; bukan zip."""
    kand = []
    for a in anchors:
        t, h = a["text"].lower(), a["href"].lower()
        if re.search(r"\bzip\b", t) or "/zip/" in h or h.endswith(".zip"):
            continue
        skor = 0
        if "/pdf/" in h or h.endswith(".pdf") or "downloadpdf" in h:
            skor = 3
        elif t.endswith(".pdf") or re.search(r"\bpdf\b", t):
            skor = 2
        elif "download_file" in h or "/download/" in h:
            skor = 1
        if skor:
            kand.append((skor, a["href"]))
    return max(kand, key=lambda x: x[0])[1] if kand else None


def halaman_daftar_valid(response, tahun):
    if not response:
        return ["halaman gagal dimuat"]
    url = response.url
    teks = teks_body(response)
    n_detail = sum(1 for h in semua_href(response) if RE_DETAIL.search(h))
    masalah = []
    if url.rstrip("/").endswith("/direktori.html") or url.rstrip("/") == BASE:
        masalah.append("URL masih beranda direktori")
    if "/tahun/" in url and f"/tahun/{tahun}" not in url:
        masalah.append(f"URL bukan tahun {tahun}")
    if "pn-surabaya" not in url and "surabaya" not in teks:
        masalah.append("bukan daftar PN Surabaya")
    if n_detail == 0:
        masalah.append("tidak ada link putusan")
    return masalah


def unduh_pdf(session, href_pdf, tujuan, detail_url):
    """Unduh PDF sebagai browser download, bukan navigasi halaman."""
    info = []

    try:
        page = session.context.new_page()
        try:
            with page.expect_download(timeout=120000) as download_info:
                try:
                    page.goto(href_pdf, referer=detail_url, timeout=120000)
                except Exception as e:
                    if "Download is starting" not in str(e):
                        raise
            download_info.value.save_as(tujuan)
        finally:
            page.close()
        with open(tujuan, "rb") as f:
            if f.read(4) == b"%PDF":
                return True, ""
        if os.path.exists(tujuan):
            os.remove(tujuan)
        info.append("download: file bukan PDF")
    except Exception as e:
        info.append("download: " + str(e).splitlines()[0][:60])
    return False, " | ".join(info)


def dump_debug(response, out, nama):
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, nama)
    with open(path, "w", encoding="utf-8") as f:
        f.write(response.body.decode("utf-8", errors="replace"))
    print(f"   [debug] HTML halaman disimpan: {path}")
    rel = [a for a in semua_anchor(response) if re.search(r"pdf|zip|download|unduh|file", a["href"] + a["text"], re.I)]
    for a in rel[:15]:
        print("   [debug] anchor:", repr(a["text"][:50]), "->", a["href"][:120])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tahun", nargs="+", type=int, default=[2023, 2024, 2025])
    ap.add_argument("--target", type=int, default=50)
    ap.add_argument("--out", default="hasil_crawl_surabaya")
    ap.add_argument("--batas-sesi", type=int, default=60, help="maks PDF per sesi")
    ap.add_argument("--ulang-tanpa-pdf", action="store_true", help="coba lagi putusan yang sebelumnya tidak punya tautan PDF")
    ap.add_argument("--delay-min", type=float, default=8.0)
    ap.add_argument("--delay-max", type=float, default=15.0)
    args = ap.parse_args()
    global DELAY
    DELAY = (args.delay_min, args.delay_max)

    pdf_dir = os.path.join(args.out, "pdf")
    os.makedirs(pdf_dir, exist_ok=True)
    log_path = os.path.join(args.out, "log_crawling.json")
    log = {"diunduh": [], "dilewati": []}
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            log = json.load(f)
        print(f"Melanjutkan: {len(log['diunduh'])} PDF sudah ada.")
    # alasan yang bersifat teknis boleh dicoba ulang
    ulang = ULANG + (("tautan PDF tidak ada",) if args.ulang_tanpa_pdf else ())
    log["dilewati"] = [d for d in log["dilewati"] if not d["alasan"].startswith(ulang)]
    sudah = {d["url"] for d in log["diunduh"]} | {d["url"] for d in log["dilewati"]}

    def simpan():
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=1)

    with StealthySession(
        headless=False,
        user_data_dir=os.path.join(args.out, "profil_browser"),
        disable_resources=True,
        timeout=60000,
        solve_cloudflare=True,
    ) as session:
        print("\n1) Di jendela browser: selesaikan verifikasi Cloudflare bila muncul.")
        buka(session, BASE + "/direktori.html")

        kand_path = os.path.join(args.out, "kandidat.json")
        tersimpan = {}
        if os.path.exists(kand_path):
            with open(kand_path, encoding="utf-8") as f:
                tersimpan = json.load(f)
        kandidat = []
        for tahun in args.tahun:
            print(f"\n=== TAHUN {tahun} ===")
            if str(tahun) in tersimpan:
                print(f"   Daftar tahun {tahun} sudah tersimpan ({len(tersimpan[str(tahun)])} link) -> tidak perlu membuka ulang.")
                for l in tersimpan[str(tahun)]:
                    if l not in kandidat:
                        kandidat.append(l)
                continue
            awal = URL_KATEGORI.format(tahun=tahun)
            print("   Membuka langsung:", awal)
            response = buka(session, awal)
            if not response or halaman_daftar_valid(response, tahun):
                print("   !! Daftar tahun gagal dimuat:",
                      "; ".join(halaman_daftar_valid(response, tahun)))
                continue
            if "/page/" in awal:                         # mulai dari halaman 1
                awal = basis_url(awal) + ".html"
            links_tahun = kumpulkan_link(session, awal)
            tersimpan[str(tahun)] = links_tahun
            with open(kand_path, "w", encoding="utf-8") as f:
                json.dump(tersimpan, f, indent=1)
            for l in links_tahun:
                if l not in kandidat:
                    kandidat.append(l)
            print(f"   Kandidat sejauh ini: {len(kandidat)}")
        print("\nTotal link kandidat:", len(kandidat))

        unduhan_sesi, gagal_beruntun, debug_n = 0, 0, 0
        berhenti = False
        for link in kandidat:
            if berhenti or len(log["diunduh"]) >= args.target or unduhan_sesi >= args.batas_sesi:
                break
            if link in sudah:
                continue
            response = buka(session, link, referer=None)
            if not response:
                print("  - gagal membuka halaman detail, lanjut:", link[-30:])
                gagal_beruntun += 1
                if gagal_beruntun >= 3:
                    berhenti = tanya_berhenti()
                    gagal_beruntun = 0
                continue
            if terblokir(response):
                print("\n!! Terdeteksi pembatasan/blokir. BERHENTI. Tunggu beberapa jam lalu jalankan ulang (progres tersimpan).")
                break
            jeda()
            siap(response, "table tr")
            meta = baca_meta(response)
            alasan = alasan_tolak(meta)
            if alasan:
                if alasan == "halaman tidak termuat":
                    gagal_beruntun += 1
                    if debug_n < 3:
                        dump_debug(response, args.out, f"debug_detail_{debug_n}.html"); debug_n += 1
                    if gagal_beruntun >= 3:
                        berhenti = tanya_berhenti()
                        gagal_beruntun = 0
                else:
                    gagal_beruntun = 0
                    log["dilewati"].append({"url": link, "alasan": alasan})
                    simpan()
                    print("  - dilewati:", alasan)
                continue
            gagal_beruntun = 0

            anchors = semua_anchor(response)
            pdf_href = cari_link_pdf(anchors)
            teks = str(response.css("body").first.get_all_text())
            if not pdf_href:
                log["dilewati"].append({"url": link, "alasan": "tautan PDF tidak ada"})
                simpan()
                print("  - dilewati: tautan PDF tidak ada", meta.get("nomor", ""))
                if debug_n < 3:
                    dump_debug(response, args.out, f"debug_detail_{debug_n}.html"); debug_n += 1
                continue
            slug = re.search(r"/putusan/([A-Za-z0-9]+)\.html", link).group(1)
            tujuan = os.path.join(pdf_dir, slug + ".pdf")
            if not os.path.exists(tujuan):
                ok, ket = unduh_pdf(session, pdf_href, tujuan, response.url)
                if not ok:
                    log["dilewati"].append({"url": link, "alasan": "gagal unduh PDF", "detail": ket})
                    simpan()
                    print("  - dilewati: gagal unduh PDF", meta.get("nomor", ""))
                    print("      ", ket)
                    continue
            log["diunduh"].append({"url": link, "file": slug + ".pdf", "meta": meta,
                                   "putusan_terkait": re.findall(r"(?:Banding|Kasasi|Pertama|Peninjauan Kembali)\s*:\s*[^\n]+", teks)})
            unduhan_sesi += 1
            simpan()
            print(f"[{len(log['diunduh']):02d}/{args.target}] {slug}.pdf  {meta.get('nomor', '')}")
            jeda()

    simpan()
    print(f"\nSelesai. Total diunduh: {len(log['diunduh'])}, dilewati: {len(log['dilewati'])}")
    for a, n in Counter(re.sub(r"\s*\(.*", "", d["alasan"]) for d in log["dilewati"]).most_common():
        print(f"   {n:3d} x {a}")
if __name__ == "__main__":
    main()