import vitis

client = vitis.create_client()
client.set_workspace(path="/net/sdeulm4824/users1/HG152832/git_ws/itch_fpga/vitis")
platform = client.create_platform_component(
    name="zcu102_itch",
    hw_design="/net/sdeulm4824/users1/HG152832/git_ws/itch_fpga/itch/design_1_wrapper.xsa",
    os="standalone",
    cpu="psu_cortexa53_0",
    domain_name="standalone_psu_cortexa53_0",
)
platform.build()
vitis.dispose()