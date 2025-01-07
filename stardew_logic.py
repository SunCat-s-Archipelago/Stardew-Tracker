try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    from yaml import Loader, Dumper
import yaml


def main():
    with open("Stardew Valley.yaml", "r") as document:
        yaml_data = yaml.load(document, Loader)
        print(yaml.dump(yaml_data))


if __name__ == '__main__':
    main()