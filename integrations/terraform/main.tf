terraform {
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
  }
}

variable "image" {
  type    = string
  default = "ghcr.io/viperisuseful/vipercapture:v0.2.0-beta.3"
}

variable "port" {
  type    = number
  default = 8765
}

variable "admin_token" {
  type      = string
  sensitive = true
}

variable "control_secret" {
  type      = string
  sensitive = true
}

resource "docker_image" "vipercapture" {
  name = var.image
}

resource "docker_volume" "vipercapture_data" {
  name = "vipercapture-data"
}

resource "docker_container" "vipercapture" {
  name    = "vipercapture"
  image   = docker_image.vipercapture.image_id
  restart = "unless-stopped"

  ports {
    internal = 8000
    external = var.port
    ip       = "127.0.0.1"
  }

  volumes {
    volume_name    = docker_volume.vipercapture_data.name
    container_path = "/data"
  }

  env = [
    "VIPERCAPTURE_ADMIN_TOKEN=${var.admin_token}",
    "VIPERCAPTURE_CONTROL_SECRET=${var.control_secret}",
  ]
}
