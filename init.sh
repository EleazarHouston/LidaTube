#!/bin/sh

echo -e "\033[1;32mFork of TheWicklowWolf/LidaTube\033[0m"
echo -e "\033[1;34mLidaTube\033[0m"
echo "Initializing app..."

cat << 'EOF'
_____________________________________

               .-'''''-.             
             .'         `.           
            :             :          
           :               :         
           :      _/|      :         
            :   =/_/      :          
             `._/ |     .'           
          (   /  ,|...-'             
           \_/^\/||__                
       _/~  `""~`"` \_               
     __/  -'/  `-._ `\_\__           
    /    /-'`  `\   \  \-.\          
_____________________________________
Original project by TheWicklowWolf
_____________________________________

If you'd like to support the original project:
https://buymeacoffee.com/thewicklow
Fork maintained separately at: https://github.com/EleazarHouston/LidaTube

EOF

echo "-----------------"
echo -e "\033[1mInstalled Versions\033[0m"
# Get the version of yt-dlp
echo -n "yt-dlp: "
pip show yt-dlp | grep Version: | awk '{print $2}'

# Get the version of ffmpeg
echo -n "FFmpeg: "
ffmpeg -version | head -n 1 | awk '{print $3}'
echo "-----------------"

PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "-----------------"
echo -e "\033[1mRunning with:\033[0m"
echo "PUID=${PUID}"
echo "PGID=${PGID}"
echo "-----------------"

# Create the required directories with the correct permissions
echo "Setting up directories.."
mkdir -p /lidatube/downloads /lidatube/config /lidatube/cache
# Avoid recursively traversing the whole app path (especially large downloads mounts)
# during startup. Set direct ownership for writable paths and recurse only on small dirs.
chown ${PUID}:${PGID} /lidatube/downloads /lidatube/config /lidatube/cache 2>/dev/null || true
chown -R ${PUID}:${PGID} /lidatube/config /lidatube/cache 2>/dev/null || true

# Set XDG_CACHE_HOME to use the cache directory
export XDG_CACHE_HOME=/lidatube/cache

# Start the application with the specified user permissions
echo "Running LidaTube..."
exec su-exec ${PUID}:${PGID} gunicorn src.LidaTube:app -c gunicorn_config.py
