{ pkgs }: {
  deps = [
    # ffmpeg-full включает проприетарные кодеки, такие как libx264, необходимые для -c:v libx264
    pkgs.ffmpeg-full
    pkgs.imagemagick
  ];
}