// 前端只放 anon key（公开安全，依赖 RLS）。绝不能放 service_role key。
// 部署时 GitHub Actions 会用仓库 Variables 覆盖本文件。
export const SUPABASE_URL = "https://oiymgpargbesvcfxyksj.supabase.co";
export const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9peW1ncGFyZ2Jlc3ZjZnh5a3NqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTE5NTMxMTAsImV4cCI6MjEwNzUyOTExMH0.Pc-aNYcsvQBC_-Lq8y9kVth2deY4uTckyAkrp45e5Z0";
// 也可改用新式 publishable key: sb_publishable_chD9FEQJqMtiUpfn1KMW3A__j12mW3F
