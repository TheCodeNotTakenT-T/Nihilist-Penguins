import matplotlib.pyplot as plt

def plot_temperature(temp):
    plt.figure(figsize=(10,8))
    plt.imshow(temp, cmap="inferno")
    plt.colorbar(label="Temperature (°C)")
    plt.title("Brightness Temperature")
    plt.tight_layout()
    plt.show()

def plot_histogram(temp):
    plt.figure(figsize=(8,6))
    plt.hist(temp.flatten(), bins=50, color='orange', edgecolor='black')
    plt.title("Temperature Histogram")
    plt.xlabel("Temperature (°C)")
    plt.ylabel("Frequency")
    plt.tight_layout()
    plt.show()
