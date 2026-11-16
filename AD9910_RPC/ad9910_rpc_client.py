from sipyco.pc_rpc import Client

client = Client("192.168.1.20", 3250, "ad9910")
client.set_frequency(0x0422, 2, 100.0)
client.set_power(0x0422, 2, 0.09)
client.set_phase(0x0422, 2, 0.2)
# client.adjust_amplitude("0x0422", 2, -0.1)

client.close()
