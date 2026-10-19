variable "subscription_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f-]{36}$", var.subscription_id))
    error_message = "An explicit destination subscription UUID is required."
  }
}
variable "tenant_id" {
  type = string
}
variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{5,15}$", var.name))
    error_message = "Use 6-16 lowercase alphanumeric characters, starting with a letter."
  }
}
variable "web_location" {
  type    = string
  default = "koreacentral"
}
variable "gpu_location" {
  type    = string
  default = "uksouth"
  validation {
    condition     = var.gpu_location == "uksouth"
    error_message = "The NVFP4 profile currently shares the UK South cluster; other regions need a profile change."
  }
}
variable "web_sku" {
  type    = string
  default = "B1"
  validation {
    condition     = var.web_sku == "B1"
    error_message = "Only the reviewed Linux B1 capacity-one hosting plan is supported."
  }
}
