terraform {
  required_providers { docker = { source = "kreuzwerker/docker", version = "~> 3.0" } }
}

variable "image" { type = string; default = "ghcr.io/viperisuseful/vipercapture:latest" }
variable "port" { type = number; default = 8765 }
variable "admin_token" { type = string; sensitive = true }

resource "docker_image" "vipercapture" { name = var.image }
resource "docker_container" "vipercapture" {
  name  = "vipercapture"
  image = docker_image.vipercapture.image_id
  restart = "unless-stopped"
  ports { internal = 8765; external = var.port; ip = "127.0.0.1" }
  env = ["VIPERCAPTURE_ADMIN_TOKEN=${var.admin_token}"]
}
