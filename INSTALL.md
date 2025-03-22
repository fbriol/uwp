# Installation Guide

This guide outlines the steps to build and set up the project.

## Prerequisites

Before you begin, ensure you have the following installed:

* **CMake:** Version specified in [CMakeLists.txt](CMakeLists.txt).
* **osmium-tool:** Follow the installation instructions at
  [https://github.com/osmcode/osmium-tool](https://github.com/osmcode/osmium-tool).
* **GDAL:** Follow the installation instructions at
  [https://github.com/OSGeo/gdal](https://github.com/OSGeo/gdal).
* **Python 3:** Ensure Python 3 is installed and accessible in your environment.

## Building the C++ Program

1.  **Navigate to the project directory:**

    ```bash
    cd <path to your project>
    ```

2.  **Create a build directory:**

    ```bash
    mkdir build
    cd build
    ```

3.  **Configure the project with CMake:**

    ```bash
    cmake ..
    ```

4.  **Build the project:**

    ```bash
    make
    ```

    or, to use all available cores for faster building:

    ```bash
    make -j$(nproc)
    ```

    > For Windows, launch the compilation by opening the generated Visual Studio
    > solution file or by running `cmake --build .` from the Command Prompt.

5. **Locate the compiled executable:**

    The executable will be located in the `build` directory (or a subdirectory
    within, depending on your CMake configuration).

## Setting Up the Environment

Make sure that `osmium-tool`, `GDAL` executables, and the compiled C++ program
are accessible from your command line. You might need to add their directories
to your `PATH` environment variable.  For example, on macOS:

```bash
export PATH="/path/to/osmium-tool:/path/to/gdal:/path/to/cpp/executable:$PATH"
```

Adjust the paths accordingly.  You may want to add this line to your `.bashrc`
or `.zshrc` file to make it permanent.

## Usage

After completing the above steps, you should be able to run the provided scripts
to process the update of water polygons. Refer to the script documentation or
inline comments for specific usage instructions.  Since there is no formal
setup, ensure all dependencies are correctly configured in your environment
before running the scripts.

> See also [README.md](README.md)