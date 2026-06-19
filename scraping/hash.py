import hashlib
url = "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/departamento-en-venta-de-2-dorm-en-santiago/32159226"
print(hashlib.md5(url.encode()).hexdigest() + ".html")