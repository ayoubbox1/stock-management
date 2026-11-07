from app import app

# Vercel WSGI entry point
# Expose `app` for @vercel/python runtime
if __name__ == "__main__":
    app.run()
